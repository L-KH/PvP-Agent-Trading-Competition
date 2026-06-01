"""Offline unit tests for the v3 strategy: signal math (incl. RSI + structure)
and every decide() branch (reversal entry gates + exit management).

Run:  python test_strategy.py     (or: pytest test_strategy.py)
No network or chain access required.
"""
from __future__ import annotations

from config import Config
from strategy import (
    Portfolio, Signals, Snapshot, build_signals, compute_flow_imbalance,
    compute_momentum, compute_rsi, compute_structure, compute_volatility,
    compute_vwap, decide, _parse_trades,
)
from utils import to_wei18

SAMPLE_TRADES = [
    {"timestamp": 1780307056, "is_buy": 0, "amount_in": "417447630658828617690",
     "amount_out": "31710963732346314793", "price": "0.075963932726840734"},
    {"timestamp": 1780307056, "is_buy": 0, "amount_in": "474005000000000000",
     "amount_out": "34949563419257585", "price": "0.073732478390011888"},
    {"timestamp": 1780307055, "is_buy": 1, "amount_in": "3411917000000000000",
     "amount_out": "43428948533356507449", "price": "0.078563196099012302"},
    {"timestamp": 1780307055, "is_buy": 1, "amount_in": "7119657000000000000",
     "amount_out": "89749010254434981682", "price": "0.079328529415712185"},
    {"timestamp": 1780307050, "is_buy": 0, "amount_in": "1342020506243695927690",
     "amount_out": "109355256247191181401", "price": "0.081485532999250228"},
]


def _cfg(**over) -> Config:
    cfg = Config()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _sig(price=100.0, vwap=100.0, rsi=50.0, drawdown=0.0, bounce=0.0,
         short_momentum=0.0, flow=0.0, momentum=0.0, vol=0.02) -> Signals:
    return Signals(price=price, vwap=vwap, momentum=momentum, flow_imbalance=flow,
                   price_dev=(price - vwap) / vwap if vwap else 0.0, volatility=vol,
                   rsi=rsi, recent_low=0.0, recent_high=0.0, drawdown=drawdown,
                   bounce=bounce, short_momentum=short_momentum, n_trades=50)


def _snap(sig, gr) -> Snapshot:
    return Snapshot("0xtok", sig.price, sig, gr)


def _port(usdc, token, cum=0.0, avg_entry=0.0, peak=0.0) -> Portfolio:
    return Portfolio(usdc, token, cum, avg_entry, peak)


def _series(prices):
    return [{"ts": i, "price": p, "is_buy": True, "usdc": 1, "token": 1}
            for i, p in enumerate(prices)]


# ── signal math ───────────────────────────────────────────────────────────────
def test_parse_orders_chronologically():
    parsed = _parse_trades(SAMPLE_TRADES)
    assert len(parsed) == 5 and [r["ts"] for r in parsed] == sorted(r["ts"] for r in parsed)


def test_vwap_within_range():
    parsed = _parse_trades(SAMPLE_TRADES)
    vwap = compute_vwap(parsed, 30)
    assert min(r["price"] for r in parsed) <= vwap <= max(r["price"] for r in parsed)


def test_momentum_sign():
    assert compute_momentum(_series(range(100, 110)), 10) > 0
    assert compute_momentum(_series(range(110, 100, -1)), 10) < 0


def test_flow_imbalance_extremes():
    buys = [{"ts": i, "price": 1, "is_buy": True, "usdc": 10, "token": 10} for i in range(5)]
    sells = [{"ts": i, "price": 1, "is_buy": False, "usdc": 10, "token": 10} for i in range(5)]
    assert abs(compute_flow_imbalance(buys, 10) - 1.0) < 1e-9
    assert abs(compute_flow_imbalance(sells, 10) + 1.0) < 1e-9


def test_volatility():
    assert compute_volatility(_series([100] * 10), 10) == 0.0
    assert compute_volatility(_series([100, 110, 100, 110, 100, 110]), 10) > 0.0


def test_rsi_direction():
    assert compute_rsi(_series(list(range(100, 120))), 9) > 50   # rising
    assert compute_rsi(_series(list(range(120, 100, -1))), 9) < 50  # falling


def test_structure_dump_and_bounce():
    lo, hi, dd, bnc = compute_structure(_series([100, 95, 90, 80, 82]), 14)
    assert lo == 80 and hi == 100
    assert abs(dd - 0.20) < 1e-9            # (100-80)/100
    assert abs(bnc - 0.025) < 1e-9          # (82-80)/80


def test_build_signals_populates_v3_fields():
    sig = build_signals(SAMPLE_TRADES, 0.05, _cfg())
    assert sig.price == 0.05 and sig.vwap > 0
    assert 0 <= sig.rsi <= 100 and sig.drawdown >= 0 and sig.bounce >= 0


# ── entry branches (flat): reversal gates ────────────────────────────────────
def test_reversal_entry_fires():
    cfg = _cfg()
    sig = _sig(rsi=30, drawdown=0.20, bounce=0.02, short_momentum=0.01, flow=0.30)
    d = decide(_snap(sig, gr=120), _port(usdc=1000, token=0), cfg)
    assert d.action == "buy" and "REVERSAL" in d.reason


def test_no_entry_without_dump():
    cfg = _cfg()
    sig = _sig(rsi=30, drawdown=0.02, bounce=0.02, short_momentum=0.01, flow=0.30)  # no dump
    assert decide(_snap(sig, 120), _port(1000, 0), cfg).action == "hold"


def test_no_entry_when_still_falling():
    cfg = _cfg()
    sig = _sig(rsi=30, drawdown=0.20, bounce=0.02, short_momentum=-0.05, flow=0.30)  # not turning up
    assert decide(_snap(sig, 120), _port(1000, 0), cfg).action == "hold"


def test_no_entry_when_not_oversold():
    cfg = _cfg()
    sig = _sig(rsi=70, drawdown=0.20, bounce=0.02, short_momentum=0.01, flow=0.30)  # not oversold
    assert decide(_snap(sig, 120), _port(1000, 0), cfg).action == "hold"


def test_no_entry_on_sell_flow():
    cfg = _cfg()
    sig = _sig(rsi=30, drawdown=0.20, bounce=0.02, short_momentum=0.01, flow=-0.5)  # heavy selling
    assert decide(_snap(sig, 120), _port(1000, 0), cfg).action == "hold"


def test_entry_blackout_near_end():
    cfg = _cfg()
    sig = _sig(rsi=30, drawdown=0.20, bounce=0.02, short_momentum=0.01, flow=0.30)
    assert decide(_snap(sig, gr=8), _port(1000, 0), cfg).action == "hold"  # gr < no_entry(12)


def test_buy_size_clamped_by_cap():
    cfg = _cfg(buy_cap_usdc=1000, trade_size_usdc=100)
    sig = _sig(rsi=30, drawdown=0.20, bounce=0.02, short_momentum=0.01, flow=0.30)
    d = decide(_snap(sig, 120), _port(usdc=1000, token=0, cum=950), cfg)
    assert d.action == "buy" and abs(d.amount_usdc - 50.0) < 1e-9


# ── exit branches (holding) ─────────────────────────────────────────────────
def test_take_profit():
    cfg = _cfg()
    d = decide(_snap(_sig(price=105.0), gr=120), _port(0, 50, avg_entry=100.0, peak=105.0), cfg)
    assert d.action == "sell" and "TAKE-PROFIT" in d.reason


def test_stop_loss():
    cfg = _cfg()
    d = decide(_snap(_sig(price=95.0), gr=120), _port(0, 50, avg_entry=100.0, peak=100.0), cfg)
    assert d.action == "sell" and "STOP-LOSS" in d.reason


def test_trailing_stop():
    cfg = _cfg()  # peak gain 3.5% (armed), pulled back 1.6% from peak, unrl +1.8% (< TP)
    d = decide(_snap(_sig(price=101.8), gr=120), _port(0, 50, avg_entry=100.0, peak=103.5), cfg)
    assert d.action == "sell" and "TRAILING-STOP" in d.reason


def test_dissolution_exit():
    cfg = _cfg()
    d = decide(_snap(_sig(price=98.0), gr=10), _port(0, 50, avg_entry=100.0, peak=100.0), cfg)
    assert d.action == "sell" and "DISSOLUTION" in d.reason


def test_dissolution_exit_without_cost_basis():
    # Regression: a bag with unknown cost basis must still flatten at dissolution
    # (previously it held to zero -> the FROSTVAULT 4-buys/3-sells loss).
    cfg = _cfg()
    d = decide(_snap(_sig(price=98.0), gr=10), _port(0, 50, avg_entry=0.0), cfg)
    assert d.action == "sell" and "DISSOLUTION" in d.reason


def test_hold_small_unrealized():
    cfg = _cfg()
    d = decide(_snap(_sig(price=101.5), gr=120), _port(0, 50, avg_entry=100.0, peak=101.5), cfg)
    assert d.action == "hold" and "holding" in d.reason


def test_awaiting_cost_basis():
    cfg = _cfg()
    d = decide(_snap(_sig(price=100.0), gr=120), _port(0, 50, avg_entry=0.0), cfg)
    assert d.action == "hold" and "cost basis" in d.reason


def test_warmup_no_price():
    cfg = _cfg()
    sig = _sig(price=0.0, vwap=0.0)
    assert decide(_snap(sig, 120), _port(1000, 0), cfg).action == "hold"


def test_to_wei18_roundtrip():
    assert to_wei18(1) == 10 ** 18 and to_wei18(0.000001) == 10 ** 12


# ── standalone runner ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
    raise SystemExit(0 if passed == len(tests) else 1)
