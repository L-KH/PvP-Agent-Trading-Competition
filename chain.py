"""On-chain trade execution for Battleground Alpha.

Trading is NOT a REST call — it is an on-chain transaction. The platform signer
(/api/tokens/:addr/swap/signature) hands us an EIP-712 signature + a precomputed
sqrtPriceLimit; we relay that into `Trader.tradeViaFactory(...)`, dispatched
through the agent's Roles modifier:

    roles.execTransactionWithRole(TRADER, 0, calldata, operation=1, ROLE_KEY, true)

operation=1 is a DELEGATECALL into the shared Trader helper. The role permits
ONLY `tradeViaFactory` and `approveFactory`, so the signer EOA can never move
funds between Safes. The agent never constructs the swap cryptography itself —
it only forwards the server-made signature — so this module is mechanical.

Sends are serialised behind a lock so overlapping trades can't collide on the
EOA nonce.
"""
from __future__ import annotations

import threading
from typing import Optional, Tuple

from web3 import Web3

from utils import account_from_key, from_wei18

MAX_UINT256 = (1 << 256) - 1

# ── ABIs (JSON form of the human-readable ABIs in the platform template) ──────
TRADER_ABI = [
    {
        "type": "function", "name": "tradeViaFactory", "stateMutability": "nonpayable",
        "outputs": [],
        "inputs": [
            {"name": "factory", "type": "address"},
            {"name": "signature", "type": "tuple", "components": [
                {"name": "signature", "type": "bytes"},
                {"name": "data", "type": "bytes"},
                {"name": "expiresAt", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
            ]},
            {"name": "tradeLimits", "type": "tuple", "components": [
                {"name": "sqrtPriceLimit", "type": "uint160"},
                {"name": "minAmountOut", "type": "uint256"},
            ]},
            {"name": "ethValue", "type": "uint256"},
        ],
    },
    {
        "type": "function", "name": "approveFactory", "stateMutability": "nonpayable",
        "outputs": [],
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
    },
]

ROLES_ABI = [{
    "type": "function", "name": "execTransactionWithRole", "stateMutability": "nonpayable",
    "outputs": [{"name": "success", "type": "bool"}],
    "inputs": [
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "data", "type": "bytes"},
        {"name": "operation", "type": "uint8"},
        {"name": "roleKey", "type": "bytes32"},
        {"name": "shouldRevert", "type": "bool"},
    ],
}]

ERC20_ABI = [
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "allowance", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]


class ChainTrader:
    def __init__(self, cfg, state: dict, logger, rpc_url: str):
        self.cfg = cfg
        self.log = logger
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        self.account = account_from_key(state["pk"])
        self.trading_safe = Web3.to_checksum_address(state["tradingSafe"])
        self.roles = self.w3.eth.contract(
            address=Web3.to_checksum_address(state["rolesMod"]), abi=ROLES_ABI)
        self.trader_addr = Web3.to_checksum_address(cfg.trader)
        self.factory = Web3.to_checksum_address(cfg.factory)
        self.usdc = Web3.to_checksum_address(cfg.usdc)
        self.role_key = Web3.to_bytes(hexstr=cfg.role_key)
        self._trader_iface = self.w3.eth.contract(abi=TRADER_ABI)  # for encoding only
        self._lock = threading.Lock()

        try:
            connected = self.w3.is_connected()
        except Exception:
            connected = False
        self.log.info("chain: rpc=%s connected=%s chainId=%s safe=%s",
                      rpc_url, connected, cfg.chain_id, self.trading_safe)

    # ── reads ─────────────────────────────────────────────────────────────────
    def balances(self, token_address: Optional[str]) -> Tuple[int, int]:
        """(usdc_wei, token_wei) held by the Trading Safe. token_wei is 0 when
        token_address is None."""
        usdc_c = self.w3.eth.contract(address=self.usdc, abi=ERC20_ABI)
        usdc_wei = usdc_c.functions.balanceOf(self.trading_safe).call()
        token_wei = 0
        if token_address:
            tok = self.w3.eth.contract(
                address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
            token_wei = tok.functions.balanceOf(self.trading_safe).call()
        return int(usdc_wei), int(token_wei)

    def eth_balance(self, address: Optional[str] = None) -> int:
        return int(self.w3.eth.get_balance(
            Web3.to_checksum_address(address or self.account.address)))

    # ── writes ─────────────────────────────────────────────────────────────────
    def approve_factory(self, token_address: str):
        """Approve the factory to pull the battle token so later sells settle."""
        calldata = self._encode("approveFactory",
                                [Web3.to_checksum_address(token_address), MAX_UINT256])
        self.log.info("approving battle token %s", token_address)
        return self._exec(calldata)

    def trade(self, token_address: str, amount_wei: int, is_buy: bool, sig: dict):
        """Relay a platform-signed swap on-chain. `sig` is the dict returned by
        BIDClient.get_swap_signature()."""
        side = "BUY" if is_buy else "SELL"
        unit = "USDC" if is_buy else "tokens"
        self.log.info("%s %.6f %s of %s — dispatching",
                      side, from_wei18(amount_wei), unit, token_address)

        signature, data, expires_at, nonce, sqrt_price_limit = self._unpack_sig(sig)
        signature_tuple = (
            Web3.to_bytes(hexstr=signature),
            Web3.to_bytes(hexstr=data),
            expires_at,
            nonce,
        )
        limits_tuple = (sqrt_price_limit, 0)  # minAmountOut=0: thin pool
        calldata = self._encode(
            "tradeViaFactory", [self.factory, signature_tuple, limits_tuple, 0])
        return self._exec(calldata)

    @staticmethod
    def _unpack_sig(sig: dict):
        """Normalise the swap-signature response into
        (signature, data, expiresAt, nonce, sqrtPriceLimit).

        /api/tokens/:addr/swap/signature nests the EIP-712 parts under a
        `signature` object; the older /skill/swap returns them flat. Handle both.
        """
        inner = sig.get("signature")
        parts = inner if isinstance(inner, dict) else sig
        signature = parts["signature"]
        data = parts["data"]
        expires_at = int(parts.get("expiresAt", sig.get("expiresAt")))
        nonce = int(parts.get("nonce", sig.get("nonce")))
        sqrt = sig.get("sqrtPriceLimit")
        if sqrt is None and isinstance(inner, dict):
            sqrt = inner.get("sqrtPriceLimit")
        return signature, data, expires_at, nonce, int(sqrt)

    # ── internals ──────────────────────────────────────────────────────────────
    def _encode(self, fn_name: str, args: list) -> str:
        c = self._trader_iface
        if hasattr(c, "encodeABI"):              # web3 v6
            return c.encodeABI(fn_name=fn_name, args=args)
        return c.encode_abi(fn_name, args=args)  # web3 v7 fallback

    def _exec(self, calldata, operation: int = 1):
        """Send execTransactionWithRole(TRADER, 0, calldata, op, ROLE_KEY, true).

        Serialised + nonce-managed. Returns the receipt (or None in DRY_RUN).
        """
        data_bytes = Web3.to_bytes(hexstr=calldata) if isinstance(calldata, str) else calldata

        if self.cfg.dry_run:
            self.log.info("[DRY_RUN] would exec role tx -> %s (%d bytes calldata)",
                          self.trader_addr, len(data_bytes))
            return None

        with self._lock:
            fn = self.roles.functions.execTransactionWithRole(
                self.trader_addr, 0, data_bytes, operation, self.role_key, True)

            # Pre-flight simulate. If estimate_gas reverts, the swap is not
            # viable right now (pool moved / sig stale) — SKIP rather than
            # force-send a doomed tx that reverts on-chain and wastes gas. The
            # loop retries next tick with a fresh signature.
            try:
                gas_est = fn.estimate_gas({"from": self.account.address})
            except Exception as exc:
                raise ChainError(f"pre-flight revert, skipping: {getattr(exc, 'message', exc)}")

            tx = fn.build_transaction({
                "from": self.account.address,
                "nonce": self.w3.eth.get_transaction_count(self.account.address, "pending"),
                "chainId": self.cfg.chain_id,
                "gasPrice": self.w3.eth.gas_price,
                "gas": int(gas_est * 1.5),  # headroom for state drift before mining
            })

            signed = self.account.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
            tx_hash = self.w3.eth.send_raw_transaction(raw)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        status = receipt.get("status") if isinstance(receipt, dict) else getattr(receipt, "status", None)
        tx_hex = Web3.to_hex(tx_hash)
        if status == 0:
            raise ChainError(f"trade reverted on-chain: {tx_hex}")
        self.log.info("  trade landed: %s", tx_hex)
        return receipt


class ChainError(Exception):
    """An on-chain transaction failed or reverted."""
