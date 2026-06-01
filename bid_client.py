"""HTTP client for the Battleground Alpha REST API (https://alpha.creator.bid).

This wraps every endpoint the agent needs:

    POST /auth/login                         {code} -> user JWT | {address,signature} -> agent JWT
    POST /auth/nonce                         {address} -> {message}
    POST /agents/register                    (Bearer user JWT) -> identity + Safes + agent JWT
    POST /agents/heartbeat                   liveness ping
    POST /agents/refill                      top up to 100k USDC + 0.5 ETH
    GET  /game                               game status + current battle token
    GET  /tokens/:addr/trades?limit=N        recent indexed swaps
    POST /tokens/:addr/swap/signature        (Bearer agent JWT) EIP-712 sig for on-chain trade

The on-chain execution of the swap lives in chain.py — this module only talks
to the REST API.
"""
from __future__ import annotations

import time
from typing import List, Optional

import requests

from utils import sign_siwe_message


class BIDAPIError(Exception):
    """A non-2xx response from the platform API."""

    def __init__(self, status: int, message: str, body=None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.body = body


class BIDClient:
    def __init__(self, cfg, logger):
        self.cfg = cfg
        self.log = logger
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    # ── core request wrapper ─────────────────────────────────────────────────
    def _request(self, method: str, path: str, token: Optional[str] = None,
                 json_body: Optional[dict] = None):
        url = self.cfg.api_base + path
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                resp = self.session.request(
                    method, url, headers=headers, json=json_body,
                    timeout=self.cfg.request_timeout_s,
                )
            except requests.RequestException as exc:
                last_exc = exc
                self.log.debug("%s %s network error (try %d): %s",
                               method, path, attempt, exc)
                time.sleep(min(2 ** attempt * 0.25, 3.0))
                continue

            # Parse body (may be JSON or raw text).
            try:
                data = resp.json()
            except ValueError:
                data = {"_raw": resp.text}

            if resp.ok:
                return data

            message = ""
            if isinstance(data, dict):
                message = data.get("error") or data.get("message") or data.get("_raw", "")
            message = (message or resp.text or "").strip()[:300]

            # Retry transient server errors; surface everything else immediately.
            if resp.status_code >= 500 and attempt < self.cfg.max_retries:
                self.log.debug("%s %s -> %d (try %d): %s",
                               method, path, resp.status_code, attempt, message)
                time.sleep(min(2 ** attempt * 0.25, 3.0))
                continue
            raise BIDAPIError(resp.status_code, message, body=data)

        raise BIDAPIError(0, f"request failed after retries: {last_exc}")

    @staticmethod
    def _extract_token(data: dict) -> str:
        for key in ("token", "jwt", "accessToken", "access_token"):
            if isinstance(data, dict) and data.get(key):
                return data[key]
        raise BIDAPIError(0, f"no token field in response: {data}")

    # ── auth ─────────────────────────────────────────────────────────────────
    def login_with_code(self, code: str) -> str:
        """Exchange a dashboard access code for a user JWT."""
        data = self._request("POST", "/auth/login", json_body={"code": code})
        return self._extract_token(data)

    def get_nonce(self, address: str) -> str:
        data = self._request("POST", "/auth/nonce", json_body={"address": address})
        msg = data.get("message") or data.get("nonce") if isinstance(data, dict) else None
        if not msg:
            raise BIDAPIError(0, f"no nonce/message in response: {data}")
        return msg

    def siwe_login(self, account) -> str:
        """SIWE: fetch a nonce, sign it with the EOA, exchange for an agent JWT."""
        message = self.get_nonce(account.address)
        signature = sign_siwe_message(account, message)
        data = self._request("POST", "/auth/login",
                             json_body={"address": account.address, "signature": signature})
        return self._extract_token(data)

    # ── agents ───────────────────────────────────────────────────────────────
    def register(self, account, user_jwt: str, name: str, archetype: str) -> dict:
        """One-time registration. Airdrops 100k USDC + 0.5 ETH and provisions
        the Trading/Treasury Safes + Roles modifier."""
        return self._request(
            "POST", "/agents/register", token=user_jwt,
            json_body={"name": name, "address": account.address, "archetype": archetype},
        )

    def heartbeat(self, address: str) -> bool:
        try:
            self._request("POST", "/agents/heartbeat", json_body={"address": address})
            return True
        except Exception as exc:  # heartbeat must never crash the agent
            self.log.debug("heartbeat failed: %s", exc)
            return False

    def refill(self, address: str) -> bool:
        try:
            self._request("POST", "/agents/refill", json_body={"address": address})
            self.log.info("refill requested for %s (top up to 100k USDC + 0.5 ETH)", address)
            return True
        except Exception as exc:
            self.log.warning("refill failed: %s", exc)
            return False

    # ── market data ──────────────────────────────────────────────────────────
    def get_game(self) -> dict:
        return self._request("GET", "/game")

    def get_trades(self, token_address: str, limit: int) -> List[dict]:
        data = self._request("GET", f"/tokens/{token_address}/trades?limit={limit}")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):  # tolerate {trades:[...]} shapes
            for key in ("trades", "data", "result"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    # ── trading ──────────────────────────────────────────────────────────────
    def get_swap_signature(self, token_address: str, amount_in_wei: int,
                           is_buy: bool, agent_jwt: str) -> dict:
        """Ask the platform signer for the EIP-712 signature + sqrtPriceLimit
        that Trader.tradeViaFactory expects. Raises BIDAPIError (e.g. 401 stale
        JWT, 403 cap reached / market-making) for the caller to handle."""
        return self._request(
            "POST", f"/tokens/{token_address}/swap/signature", token=agent_jwt,
            json_body={
                "tokenAddress": token_address,
                "amountIn": str(amount_in_wei),
                "isBuy": bool(is_buy),
            },
        )
