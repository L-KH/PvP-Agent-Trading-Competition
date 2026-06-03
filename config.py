"""Configuration management for the Battleground Alpha agent.

Precedence (highest first): real environment variables  ->  .env file  ->
config.json  ->  built-in defaults below. All on-chain contract / RPC values
are *also* refreshed from GET /api/game at runtime (see main.py), so the
hard-coded constants here are only fallbacks.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from typing import Optional


# ── Verified platform constants (fallbacks; /api/game is authoritative) ───────
DEFAULT_API_BASE = "https://alpha.creator.bid/api"
# NOTE: /api/game advertises rpcUrl "http://alpha.creator.bid:8545", but that
# host is Cloudflare-fronted and does NOT serve JSON-RPC. The raw node IP from
# the platform spec is the one that actually responds. The agent probes for a
# reachable RPC at startup (main._pick_working_rpc); this is the default/fallback.
DEFAULT_RPC_URL = "http://5.161.35.78:8545"
DEFAULT_CHAIN_ID = 42069
DEFAULT_FACTORY = "0xE841bCA5A85C76FA667a968C4fe817Ffa2E220e7"
DEFAULT_USDC = "0xed38c197b319fdc067f4c3fb58eec1a733a36cf4"
# Shared per-chain Trader helper stack — identical for every self-hosted agent.
DEFAULT_TRADER = "0x521FAcaAB630E30614617c9ae5f6508cB4213540"
DEFAULT_ROLE_KEY = "0xfacaf2747a7486cf5730e9265973fb54447d3ace6e7e4711f6360826b0731941"

# The platform's hard per-battle cumulative-buy cap. Our self-imposed cap
# (buy_cap_usdc) must never exceed this.
PLATFORM_BUY_CAP_USDC = 100_000.0


@dataclass
class Config:
    # ── API / chain (refreshed from /api/game at startup) ──
    api_base: str = DEFAULT_API_BASE
    rpc_url: str = DEFAULT_RPC_URL
    chain_id: int = DEFAULT_CHAIN_ID
    factory: str = DEFAULT_FACTORY
    usdc: str = DEFAULT_USDC
    trader: str = DEFAULT_TRADER
    role_key: str = DEFAULT_ROLE_KEY

    # ── Identity / auth ──
    access_code: Optional[str] = None   # exchanged for a user JWT on first run
    user_jwt: Optional[str] = None      # alternative to access_code
    private_key: Optional[str] = None   # optional: reuse a specific wallet
    agent_name: Optional[str] = None    # optional dashboard label
    archetype: str = "Custom"
    state_file: str = ".agent.json"

    # ── Signals ──
    momentum_window: int = 30           # window for VWAP / volatility
    trades_limit: int = 100             # recent trades fetched per tick
    min_volatility: float = 0.001       # floor for per-trade return stdev (avoid /0)
    short_window: int = 5               # fast momentum / turn-detection window
    struct_window: int = 14             # swing-structure window (sweep + bottom)
    rsi_period: int = 9                 # RSI lookback (short = fast reaction)

    # ── Entry: buy CONFIRMED reversals off the bottom (ICT sweep + MSS / RSI) ──
    fee_pct: float = 0.003              # pool fee per side (feeTier 3000 = 0.3%)
    slippage_pct: float = 0.01          # estimated slippage per side (thin pool)
    min_profit_pct: float = 0.01        # take-profit must beat round-trip cost by this
    min_drawdown: float = 0.06          # require a >=6% dump (liquidity sweep) first
    min_bounce: float = 0.0             # require price >= this above the low (0 = off; RSI guards chasing)
    drawdown_ref: float = 0.25          # drawdown normalization for the confidence score
    rsi_buy_max: float = 48.0           # only buy when oversold (RSI below this)
    flow_min: float = 0.0               # require non-negative buy pressure to enter
    min_entry_momentum: float = 0.0     # short momentum must be >= this (turning up)
    confidence_threshold: float = 0.35  # min composite score [0,1] to enter (lower = more trades)

    # ── Exit: sell into strength + risk management (reversal strategy) ──
    take_profit_pct: float = 0.05       # lock gains at +this unrealized ("sell mid")
    stop_loss_pct: float = 0.04         # cut losers at -this unrealized
    trail_activate_pct: float = 0.03    # arm trailing stop once peak gain >= this
    trailing_stop_pct: float = 0.015    # exit if price drops this far from the peak

    # ── Strategy selector + "open_pump" (buy the open, ride the early pump) ──
    # These launch tokens reliably pump ~3-5x in the first ~20s, then bleed down.
    # "open_pump" buys big at the session open and rides that pump.
    strategy: str = "open_pump"         # "open_pump" | "reversal"
    open_buy_usdc: float = 1000.0       # open-buy size (clamped to budget/Safe => ~all-in)
    open_entry_min_gr: int = 150        # only open-buy while gameRemaining >= this (early in live)
    pump_target_mult: float = 3.0       # hard take-profit at entry x this
    pump_trail_arm: float = 1.2         # arm the pump trailing-stop once price >= entry x this
    open_trail_pct: float = 0.12        # sell if price drops this far from the peak (tight = exit fast)
    open_stop_pct: float = 0.12         # cut if price falls this far below entry (failed pump)
    open_exit_by_gr: int = 120          # backstop: exit the open play once gameRemaining <= this
                                        # (the early pump is over by then; endgame is a dead bleed)
    open_hold_seconds: int = 12         # sell ~this many seconds after entry (fast exit "after N candles")

    # ── Sizing / risk ──
    trade_size_usdc: float = 100.0      # USDC per entry
    buy_cap_usdc: float = 1000.0        # self-imposed cumulative buy cap per battle
    min_trade_usdc: float = 5.0         # skip dust buys
    min_token_sell: float = 1e-9        # skip dust sells

    # ── Timing ──
    exit_seconds: int = 15              # flatten when gameRemaining < this
    no_entry_seconds: int = 12          # no new entries when gameRemaining < this
    poll_interval_s: float = 0.4        # decide() cadence while live (fast reaction)
    open_poll_s: float = 0.15           # fast poll cadence as the session open approaches
    open_poll_window: int = 6           # start fast-polling this many seconds before live opens
    loss_cooldown_s: int = 8            # pause new entries this long after a stop-loss
    heartbeat_s: int = 30               # liveness ping interval (<= 60)
    funding_wait_s: int = 6             # wait after register for the airdrop to land

    # ── Ops ──
    auto_refill: bool = True            # call /agents/refill if the safe is empty
    dry_run: bool = False
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_file: str = "agent.log"
    request_timeout_s: float = 15.0
    max_retries: int = 3

    # ────────────────────────────────────────────────────────────────────────
    def validate(self, state_exists: bool) -> None:
        """Fail fast on misconfiguration before any network calls."""
        if not state_exists and not (self.access_code or self.user_jwt):
            raise ConfigError(
                "First run needs credentials: set BID_ACCESS_CODE (from "
                "https://t.me/creatorbid) or BID_USER_JWT in your .env. "
                "Once registered, identity is cached in "
                f"'{self.state_file}' and credentials are no longer required."
            )
        if self.buy_cap_usdc > PLATFORM_BUY_CAP_USDC:
            raise ConfigError(
                f"buy_cap_usdc ({self.buy_cap_usdc}) exceeds the platform hard "
                f"cap of {PLATFORM_BUY_CAP_USDC} USDC per battle."
            )
        if self.heartbeat_s > 60:
            raise ConfigError("heartbeat_s must be <= 60 (platform requirement).")
        if self.trade_size_usdc <= 0 or self.poll_interval_s <= 0:
            raise ConfigError("trade_size_usdc and poll_interval_s must be > 0.")


class ConfigError(Exception):
    """Raised when configuration is missing or inconsistent."""


# ── env helpers ───────────────────────────────────────────────────────────────
def _load_env_file(path: str) -> None:
    """Minimal .env loader (no python-dotenv dependency). Real env vars win."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, val)
    except OSError:
        pass


def _s(name: str, default):
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def _f(name: str, default):
    val = os.environ.get(name)
    if val in (None, ""):
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _i(name: str, default):
    val = os.environ.get(name)
    if val in (None, ""):
        return default
    try:
        return int(float(val))
    except ValueError:
        return default


def _b(name: str, default):
    val = os.environ.get(name)
    if val in (None, ""):
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def load_config(env_file: str = ".env", json_file: str = "config.json") -> Config:
    """Build a Config from defaults <- config.json <- .env / environment."""
    _load_env_file(env_file)

    # Start from any keys present in config.json (ignoring unknown keys).
    data = {}
    if os.path.exists(json_file):
        try:
            with open(json_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            valid = {f.name for f in fields(Config)}
            data = {k: v for k, v in raw.items() if k in valid}
        except (OSError, json.JSONDecodeError):
            data = {}
    cfg = Config(**data)

    # Environment / .env overrides (these win over config.json).
    cfg.api_base = _s("BID_API_BASE", cfg.api_base)
    cfg.rpc_url = _s("BID_RPC_URL", cfg.rpc_url)
    cfg.chain_id = _i("BID_CHAIN_ID", cfg.chain_id)
    cfg.factory = _s("BID_FACTORY", cfg.factory)
    cfg.usdc = _s("BID_USDC", cfg.usdc)
    cfg.trader = _s("BID_TRADER", cfg.trader)
    cfg.role_key = _s("BID_ROLE_KEY", cfg.role_key)

    cfg.access_code = _s("BID_ACCESS_CODE", cfg.access_code)
    cfg.user_jwt = _s("BID_USER_JWT", cfg.user_jwt)
    cfg.private_key = _s("BID_PRIVATE_KEY", cfg.private_key)
    cfg.agent_name = _s("BID_AGENT_NAME", cfg.agent_name)
    cfg.archetype = _s("BID_ARCHETYPE", cfg.archetype)
    cfg.state_file = _s("BID_STATE_FILE", cfg.state_file)

    cfg.momentum_window = _i("BID_MOMENTUM_WINDOW", cfg.momentum_window)
    cfg.trades_limit = _i("BID_TRADES_LIMIT", cfg.trades_limit)
    cfg.min_volatility = _f("BID_MIN_VOLATILITY", cfg.min_volatility)
    cfg.short_window = _i("BID_SHORT_WINDOW", cfg.short_window)
    cfg.struct_window = _i("BID_STRUCT_WINDOW", cfg.struct_window)
    cfg.rsi_period = _i("BID_RSI_PERIOD", cfg.rsi_period)

    cfg.fee_pct = _f("BID_FEE_PCT", cfg.fee_pct)
    cfg.slippage_pct = _f("BID_SLIPPAGE_PCT", cfg.slippage_pct)
    cfg.min_profit_pct = _f("BID_MIN_PROFIT_PCT", cfg.min_profit_pct)
    cfg.min_drawdown = _f("BID_MIN_DRAWDOWN", cfg.min_drawdown)
    cfg.min_bounce = _f("BID_MIN_BOUNCE", cfg.min_bounce)
    cfg.drawdown_ref = _f("BID_DRAWDOWN_REF", cfg.drawdown_ref)
    cfg.rsi_buy_max = _f("BID_RSI_BUY_MAX", cfg.rsi_buy_max)
    cfg.flow_min = _f("BID_FLOW_MIN", cfg.flow_min)
    cfg.min_entry_momentum = _f("BID_MIN_ENTRY_MOMENTUM", cfg.min_entry_momentum)
    cfg.confidence_threshold = _f("BID_CONFIDENCE_THRESHOLD", cfg.confidence_threshold)

    cfg.take_profit_pct = _f("BID_TAKE_PROFIT_PCT", cfg.take_profit_pct)
    cfg.stop_loss_pct = _f("BID_STOP_LOSS_PCT", cfg.stop_loss_pct)
    cfg.trail_activate_pct = _f("BID_TRAIL_ACTIVATE_PCT", cfg.trail_activate_pct)
    cfg.trailing_stop_pct = _f("BID_TRAILING_STOP_PCT", cfg.trailing_stop_pct)

    cfg.strategy = _s("BID_STRATEGY", cfg.strategy)
    cfg.open_buy_usdc = _f("BID_OPEN_BUY_USDC", cfg.open_buy_usdc)
    cfg.open_entry_min_gr = _i("BID_OPEN_ENTRY_MIN_GR", cfg.open_entry_min_gr)
    cfg.pump_target_mult = _f("BID_PUMP_TARGET_MULT", cfg.pump_target_mult)
    cfg.pump_trail_arm = _f("BID_PUMP_TRAIL_ARM", cfg.pump_trail_arm)
    cfg.open_trail_pct = _f("BID_OPEN_TRAIL_PCT", cfg.open_trail_pct)
    cfg.open_stop_pct = _f("BID_OPEN_STOP_PCT", cfg.open_stop_pct)
    cfg.open_exit_by_gr = _i("BID_OPEN_EXIT_BY_GR", cfg.open_exit_by_gr)
    cfg.open_hold_seconds = _i("BID_OPEN_HOLD_SECONDS", cfg.open_hold_seconds)

    cfg.trade_size_usdc = _f("BID_TRADE_SIZE_USDC", cfg.trade_size_usdc)
    cfg.buy_cap_usdc = _f("BID_BUY_CAP_USDC", cfg.buy_cap_usdc)
    cfg.min_trade_usdc = _f("BID_MIN_TRADE_USDC", cfg.min_trade_usdc)

    cfg.exit_seconds = _i("BID_EXIT_SECONDS", cfg.exit_seconds)
    cfg.no_entry_seconds = _i("BID_NO_ENTRY_SECONDS", cfg.no_entry_seconds)
    cfg.poll_interval_s = _f("BID_POLL_INTERVAL_S", cfg.poll_interval_s)
    cfg.open_poll_s = _f("BID_OPEN_POLL_S", cfg.open_poll_s)
    cfg.open_poll_window = _i("BID_OPEN_POLL_WINDOW", cfg.open_poll_window)
    cfg.loss_cooldown_s = _i("BID_LOSS_COOLDOWN_S", cfg.loss_cooldown_s)
    cfg.heartbeat_s = _i("BID_HEARTBEAT_S", cfg.heartbeat_s)
    cfg.funding_wait_s = _i("BID_FUNDING_WAIT_S", cfg.funding_wait_s)

    cfg.auto_refill = _b("BID_AUTO_REFILL", cfg.auto_refill)
    cfg.dry_run = _b("BID_DRY_RUN", cfg.dry_run)
    cfg.log_level = _s("BID_LOG_LEVEL", cfg.log_level)

    return cfg
