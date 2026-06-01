"""Helper utilities: logging, wallet management, SIWE signing, JWT, math.

Wallet + signing are built on `eth_account` (installed as part of web3). The
agent owns a single Ethereum EOA; its private key is persisted in the state
file (.agent.json) with 0600 permissions so the agent survives restarts.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
from decimal import Decimal, getcontext
from logging.handlers import RotatingFileHandler
from typing import Optional, Tuple

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount

getcontext().prec = 78  # enough for uint256 math without float error

WEI = Decimal(10) ** 18


# ── logging ───────────────────────────────────────────────────────────────────
def setup_logging(cfg) -> logging.Logger:
    """Console + rotating file logging. Idempotent (safe to call once)."""
    logger = logging.getLogger("bid-agent")
    logger.setLevel(getattr(logging, str(cfg.log_level).upper(), logging.INFO))
    logger.propagate = False
    if logger.handlers:  # already configured
        return logger

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"
    )

    # Force UTF-8 on the console so unicode in log lines never raises
    # UnicodeEncodeError / renders as mojibake on legacy Windows code pages.
    stream = sys.stderr
    try:
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    console = logging.StreamHandler(stream)
    console.setFormatter(fmt)
    logger.addHandler(console)

    try:
        os.makedirs(cfg.log_dir, exist_ok=True)
        path = os.path.join(cfg.log_dir, cfg.log_file)
        file_handler = RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as exc:  # logging must never crash the agent
        logger.warning("file logging disabled: %s", exc)

    return logger


def now_ts() -> int:
    return int(time.time())


# ── wallet ────────────────────────────────────────────────────────────────────
def new_account() -> LocalAccount:
    """Generate a fresh Ethereum-compatible EOA."""
    return Account.create()


def account_from_key(private_key: str) -> LocalAccount:
    return Account.from_key(private_key)


def load_or_create_account(cfg, logger) -> Tuple[LocalAccount, Optional[dict]]:
    """Return (account, state).

    - If the state file exists, load the persisted wallet + Safe addresses.
    - Else build an account from cfg.private_key if provided, otherwise generate
      a brand-new one. `state` is None until registration completes.
    """
    if os.path.exists(cfg.state_file):
        state = read_state(cfg.state_file)
        acct = account_from_key(state["pk"])
        logger.info("loaded existing agent %s (EOA %s)", state.get("name"), acct.address)
        return acct, state

    if cfg.private_key:
        acct = account_from_key(cfg.private_key)
        logger.info("using wallet from BID_PRIVATE_KEY: %s", acct.address)
    else:
        acct = new_account()
        logger.info("generated new wallet EOA: %s", acct.address)
    return acct, None


def read_state(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_state(path: str, state: dict) -> None:
    """Persist agent state with restrictive permissions (contains the PK)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    try:
        os.chmod(path, 0o600)  # best-effort; no-op semantics on some Windows setups
    except OSError:
        pass


def sign_siwe_message(account: LocalAccount, message: str) -> str:
    """Sign a login challenge with the agent's EOA; returns 0x-hex signature."""
    signed = account.sign_message(encode_defunct(text=message))
    return signed.signature.hex()


# ── JWT ───────────────────────────────────────────────────────────────────────
def jwt_exp(token: str) -> int:
    """Best-effort decode of a JWT's `exp` claim (epoch seconds). 0 on failure."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return int(claims.get("exp", 0))
    except Exception:
        return 0


def jwt_expiring(token: Optional[str], skew_seconds: int = 600) -> bool:
    """True if the token is missing or expires within `skew_seconds`."""
    if not token:
        return True
    return jwt_exp(token) - now_ts() < skew_seconds


# ── math ──────────────────────────────────────────────────────────────────────
def to_wei18(amount) -> int:
    """Human float/str -> 18-decimal integer wei (exact, no float drift)."""
    return int((Decimal(str(amount)) * WEI).to_integral_value())


def from_wei18(amount_wei) -> float:
    """18-decimal integer wei -> human float."""
    return float(Decimal(int(amount_wei)) / WEI)


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if not denominator:
        return default
    return numerator / denominator


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def short_addr(addr: str) -> str:
    return addr[:6] + "…" + addr[-4:] if addr and len(addr) > 12 else (addr or "?")
