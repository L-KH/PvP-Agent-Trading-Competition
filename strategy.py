"""Trading strategy v3: buy CONFIRMED reversals off the bottom, sell into strength.

Pure module (no network/chain I/O) so it is unit-testable offline.

Why v1/v2 lost money
--------------------
Live PnL showed ~-37% over 8 battles: the agent bought tokens that were 30-50%
*below VWAP* — but on these launch tokens that means the token is **crashing**
(VWAP lags the dump), so it caught falling knives and bled to dissolution.

The winning agents buy the **bottom** (after the dump exhausts) and sell into the
bounce. This is exactly ICT's rule: the wick that sweeps liquidity is NOT the
entry — you wait for a market-structure shift (a higher low + turn up) and buy
that. v3 implements it:

  ENTRY (long, only when flat) — all gates must pass:
    * dumped:   drawdown over the structure window >= min_drawdown  (a sweep happened)
    * bounced:  price is min_bounce..max_bounce above the local low  (turning up, not chasing)
    * turning:  short-window momentum >= min_entry_momentum          (reversal underway)
    * oversold: RSI <= rsi_buy_max                                   (cheap, not extended)
    * flow_ok:  trade-flow imbalance >= flow_min                     (buyers present)
    * economic: take-profit target beats round-trip cost (2*fee + 2*slippage)
    * confident: a composite [0,1] score clears confidence_threshold
  EXIT (while holding), every tick vs our cost basis:
    * dissolution backstop, stop-loss, take-profit ("sell mid"), trailing stop
  Re-enters on the next setup -> multiple trades per battle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from utils import clamp, from_wei18, safe_div


# ── data structures ───────────────────────────────────────────────────────────
@dataclass
class Signals:
    price: float
    vwap: float
    momentum: float
    flow_imbalance: float
    price_dev: float
    volatility: float
    rsi: float
    recent_low: float
    recent_high: float
    drawdown: float        # (recent_high - recent_low) / recent_high  (the dump)
    bounce: float          # (price - recent_low) / recent_low         (recovery off low)
    short_momentum: float  # momentum over the short (fast) window
    n_trades: int


@dataclass
class Snapshot:
    token_address: str
    price: float
    signals: Signals
    game_remaining: Optional[int]
    tick: int = 0
    fresh_battle: bool = False


@dataclass
class Portfolio:
    usdc: float
    token: float
    cumulative_buys: float = 0.0
    avg_entry: float = 0.0
    peak_price: float = 0.0


@dataclass
class Decision:
    action: str            # "buy" | "sell" | "hold"
    reason: str
    amount_usdc: float = 0.0
    amount_token: float = 0.0

    @property
    def is_trade(self) -> bool:
        return self.action in ("buy", "sell")

    @property
    def is_buy(self) -> bool:
        return self.action == "buy"

    @classmethod
    def buy(cls, usdc: float, reason: str) -> "Decision":
        return cls("buy", reason, amount_usdc=usdc)

    @classmethod
    def sell(cls, token: float, reason: str) -> "Decision":
        return cls("sell", reason, amount_token=token)

    @classmethod
    def hold(cls, reason: str) -> "Decision":
        return cls("hold", reason)


# ── trade parsing + signals ───────────────────────────────────────────────────
def _parse_trades(trades: List[dict]) -> List[dict]:
    parsed = []
    for t in trades:
        try:
            price = float(t["price"])
            is_buy = bool(int(t.get("is_buy", 0)))
            amount_in = from_wei18(int(t["amount_in"]))
            amount_out = from_wei18(int(t["amount_out"]))
        except (KeyError, ValueError, TypeError):
            continue
        if is_buy:
            usdc, token = amount_in, amount_out
        else:
            token, usdc = amount_in, amount_out
        parsed.append({"ts": int(t.get("timestamp", 0)), "price": price,
                       "is_buy": is_buy, "usdc": usdc, "token": token})
    parsed.sort(key=lambda r: r["ts"])
    return parsed


def compute_momentum(parsed: List[dict], window: int) -> float:
    w = parsed[-window:]
    if len(w) < 2:
        return 0.0
    return safe_div(w[-1]["price"] - w[0]["price"], w[0]["price"])


def compute_vwap(parsed: List[dict], window: int) -> float:
    w = parsed[-window:]
    if not w:
        return 0.0
    num = sum(r["price"] * r["token"] for r in w)
    den = sum(r["token"] for r in w)
    return safe_div(num, den, default=w[-1]["price"])


def compute_flow_imbalance(parsed: List[dict], window: int) -> float:
    w = parsed[-window:]
    buy = sum(r["usdc"] for r in w if r["is_buy"])
    sell = sum(r["usdc"] for r in w if not r["is_buy"])
    return safe_div(buy - sell, buy + sell, default=0.0)


def compute_volatility(parsed: List[dict], window: int) -> float:
    w = parsed[-window:]
    rets = [(w[i]["price"] - w[i - 1]["price"]) / w[i - 1]["price"]
            for i in range(1, len(w)) if w[i - 1]["price"] > 0]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    return (sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5


def compute_rsi(parsed: List[dict], period: int) -> float:
    """Classic RSI over the last `period` price changes. 50 = neutral / no data."""
    prices = [r["price"] for r in parsed]
    if len(prices) < period + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(len(prices) - period, len(prices)):
        ch = prices[i] - prices[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - 100.0 / (1.0 + rs)


def compute_structure(parsed: List[dict], window: int) -> Tuple[float, float, float, float]:
    """(recent_low, recent_high, drawdown, bounce) over the structure window."""
    w = parsed[-window:]
    if not w:
        return 0.0, 0.0, 0.0, 0.0
    prices = [r["price"] for r in w]
    lo, hi, cur = min(prices), max(prices), prices[-1]
    return lo, hi, safe_div(hi - lo, hi), safe_div(cur - lo, lo)


def build_signals(trades: List[dict], current_price: Optional[float], cfg) -> Signals:
    parsed = _parse_trades(trades)
    vwap = compute_vwap(parsed, cfg.momentum_window)

    price = 0.0
    if current_price not in (None, "", 0):
        try:
            price = float(current_price)
        except (ValueError, TypeError):
            price = 0.0
    if price <= 0 and parsed:
        price = parsed[-1]["price"]

    lo, hi, drawdown, bounce = compute_structure(parsed, cfg.struct_window)
    return Signals(
        price=price,
        vwap=vwap,
        momentum=compute_momentum(parsed, cfg.momentum_window),
        flow_imbalance=compute_flow_imbalance(parsed, cfg.momentum_window),
        price_dev=safe_div(price - vwap, vwap),
        volatility=compute_volatility(parsed, cfg.momentum_window),
        rsi=compute_rsi(parsed, cfg.rsi_period),
        recent_low=lo,
        recent_high=hi,
        drawdown=drawdown,
        bounce=bounce,
        short_momentum=compute_momentum(parsed, cfg.short_window),
        n_trades=len(parsed),
    )


# ── the decision function ─────────────────────────────────────────────────────
def decide(snapshot: Snapshot, portfolio: Portfolio, cfg, logger=None) -> Decision:
    s = snapshot.signals
    mark = snapshot.price if snapshot.price > 0 else s.price
    gr = snapshot.game_remaining
    held = portfolio.token
    usdc = portfolio.usdc

    def out(d: Decision) -> Decision:
        if logger is not None:
            logger.debug(
                "decide[%s]: %s | mark=%.6f vwap=%.6f rsi=%.0f dd=%.3f bnc=%.3f "
                "smom=%.3f flow=%.2f gr=%s held=%.4f entry=%.6f",
                d.action, d.reason, mark, s.vwap, s.rsi, s.drawdown, s.bounce,
                s.short_momentum, s.flow_imbalance, gr, held, portfolio.avg_entry)
        return d

    # ===== Manage an open position (sell into strength + risk mgmt) =====
    if held > cfg.min_token_sell:
        # Dissolution backstop fires FIRST — never hold into round end, even if
        # the cost basis hasn't synced yet (this is what stranded a bag to zero).
        if gr is not None and gr < cfg.exit_seconds:
            return out(Decision.sell(held, f"DISSOLUTION exit gr={gr}"))
        if portfolio.avg_entry <= 0 or mark <= 0:
            return out(Decision.hold("holding; awaiting cost basis / mark"))
        unrl = (mark - portfolio.avg_entry) / portfolio.avg_entry
        if unrl <= -cfg.stop_loss_pct:
            return out(Decision.sell(held, f"STOP-LOSS unrl={unrl:.3f}"))
        if unrl >= cfg.take_profit_pct:
            return out(Decision.sell(held, f"TAKE-PROFIT unrl={unrl:.3f}"))
        if portfolio.peak_price > portfolio.avg_entry:
            peak_gain = (portfolio.peak_price - portfolio.avg_entry) / portfolio.avg_entry
            drop = safe_div(portfolio.peak_price - mark, portfolio.peak_price)
            if peak_gain >= cfg.trail_activate_pct and drop >= cfg.trailing_stop_pct:
                return out(Decision.sell(
                    held, f"TRAILING-STOP peak_gain={peak_gain:.3f} drop={drop:.3f}"))
        return out(Decision.hold(f"holding unrl={unrl:.3f}"))

    # ===== Consider a reversal entry (flat) =====
    if mark <= 0 or s.vwap <= 0:
        return out(Decision.hold("warming up - no price/vwap"))
    if gr is not None and gr < cfg.no_entry_seconds:
        return out(Decision.hold("entry blackout near round end"))

    rt_cost = 2 * cfg.fee_pct + 2 * cfg.slippage_pct
    dumped = s.drawdown >= cfg.min_drawdown
    bounced = s.bounce >= cfg.min_bounce   # floor only; RSI guards against chasing pumps
    turning = s.short_momentum >= cfg.min_entry_momentum
    oversold = s.rsi <= cfg.rsi_buy_max
    flow_ok = s.flow_imbalance >= cfg.flow_min
    economic = cfg.take_profit_pct > rt_cost + cfg.min_profit_pct

    oversold_n = clamp(safe_div(cfg.rsi_buy_max - s.rsi, cfg.rsi_buy_max), 0.0, 1.0)
    flow_n = clamp(safe_div(s.flow_imbalance - cfg.flow_min, 1.0 - cfg.flow_min), 0.0, 1.0)
    dd_n = clamp(safe_div(s.drawdown, cfg.drawdown_ref), 0.0, 1.0)
    score = (oversold_n + flow_n + dd_n) / 3.0
    confident = score >= cfg.confidence_threshold

    if dumped and bounced and turning and oversold and flow_ok and economic and confident:
        budget = max(0.0, cfg.buy_cap_usdc - portfolio.cumulative_buys)
        size = min(cfg.trade_size_usdc, budget, usdc)
        if size >= cfg.min_trade_usdc:
            return out(Decision.buy(
                size, f"REVERSAL rsi={s.rsi:.0f} dd={s.drawdown:.3f} bnc={s.bounce:.3f} "
                      f"flow={s.flow_imbalance:.2f} score={score:.2f}"))
        return out(Decision.hold("reversal signal but no budget/USDC"))

    return out(Decision.hold(
        f"no entry: dd={s.drawdown:.3f}({int(dumped)}) bnc={s.bounce:.3f}({int(bounced)}) "
        f"turn={s.short_momentum:.3f}({int(turning)}) rsi={s.rsi:.0f}({int(oversold)}) "
        f"flow={s.flow_imbalance:.2f}({int(flow_ok)}) score={score:.2f}"))
