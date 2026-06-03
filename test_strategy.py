"""Offline unit tests for both strategies + signal math.

  * signal math (parse, VWAP, momentum, flow, volatility, RSI, structure)
  * decide_reversal()  — buy confirmed bottoms, sell into the bounce
  * decide_open_pump()  — buy the open, ride the pump
  * decide()           — dispatches on cfg.strategy

Run:  python test_strategy.py     (or: pytest test_strategy.py)
No network or chain access required.
"""
from __future__ import annotations

from config import Config
from strategy import (
    Portfolio, Signals, Snapshot, build_signals, compute_flow_imbalance,
    compute_momentum, compute_rsi, compute_structure, compute_volatility,
    compute_vwap, decide, decide_open_pump, decide_reversal, _parse_trades,
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


def _snap(sig, gr, sip=0):
    return Snapshot("0xtok", sig.price, sig, gr, seconds_in_position=sip)


def _port(usdc, token, cum=0.0, avg_entry=0.0, peak=0.0):
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
    assert compute_rsi(_series(list(range(100, 120))), 9) > 50
    assert compute_rsi(_series(list(range(120, 100, -1))), 9) < 50


def test_structure_dump_and_bounce():
    lo, hi, dd, bnc = compute_structure(_series([100, 95, 90, 80, 82]), 14)
    assert lo == 80 and hi == 100
    assert abs(dd - 0.20) < 1e-9 and abs(bnc - 0.025) < 1e-9


def test_build_signals_populates_fields():
    sig = build_signals(SAMPLE_TRADES, 0.05, _cfg())
    assert sig.price == 0.05 and sig.vwap > 0
    assert 0 <= sig.rsi <= 100 and sig.drawdown >= 0 and sig.bounce >= 0


# ── reversal strategy ────────────────────────────────────────────────────────
def test_reversal_entry_fires():
    sig = _sig(rsi=30, drawdown=0.20, bounce=0.02, short_momentum=0.01, flow=0.30)
    d = decide_reversal(_snap(sig, 120), _port(1000, 0), _cfg())
    assert d.action == "buy" and "REVERSAL" in d.reason


def test_reversal_no_entry_without_dump():
    sig = _sig(rsi=30, drawdown=0.02, bounce=0.02, short_momentum=0.01, flow=0.30)
    assert decide_reversal(_snap(sig, 120), _port(1000, 0), _cfg()).action == "hold"


def test_reversal_no_entry_when_still_falling():
    sig = _sig(rsi=30, drawdown=0.20, bounce=0.02, short_momentum=-0.05, flow=0.30)
    d = decide_reversal(_snap(sig, 120), _port(1000, 0), _cfg())
    assert d.action == "hold" and "turn=-0.050(0)" in d.reason  # turning gate blocked it


def test_reversal_no_entry_when_not_oversold():
    sig = _sig(rsi=70, drawdown=0.20, bounce=0.02, short_momentum=0.01, flow=0.30)
    assert decide_reversal(_snap(sig, 120), _port(1000, 0), _cfg()).action == "hold"


def test_reversal_no_entry_on_sell_flow():
    sig = _sig(rsi=30, drawdown=0.20, bounce=0.02, short_momentum=0.01, flow=-0.5)
    assert decide_reversal(_snap(sig, 120), _port(1000, 0), _cfg()).action == "hold"


def test_reversal_take_profit():
    d = decide_reversal(_snap(_sig(price=105.0), 120), _port(0, 50, avg_entry=100.0, peak=105.0), _cfg())
    assert d.action == "sell" and "TAKE-PROFIT" in d.reason


def test_reversal_stop_loss():
    d = decide_reversal(_snap(_sig(price=95.0), 120), _port(0, 50, avg_entry=100.0, peak=100.0), _cfg())
    assert d.action == "sell" and "STOP-LOSS" in d.reason


def test_reversal_trailing_stop():
    d = decide_reversal(_snap(_sig(price=101.8), 120), _port(0, 50, avg_entry=100.0, peak=103.5), _cfg())
    assert d.action == "sell" and "TRAILING-STOP" in d.reason


def test_reversal_dissolution_without_cost_basis():
    # Regression: a bag with unknown cost basis must still flatten at dissolution.
    d = decide_reversal(_snap(_sig(price=98.0), 10), _port(0, 50, avg_entry=0.0), _cfg())
    assert d.action == "sell" and "DISSOLUTION" in d.reason


# ── open_pump strategy ───────────────────────────────────────────────────────
def test_open_buy_at_session_start():
    d = decide_open_pump(_snap(_sig(price=1.0), 178), _port(usdc=100, token=0, cum=0), _cfg())
    assert d.action == "buy" and "OPEN" in d.reason and abs(d.amount_usdc - 100) < 1e-9


def test_open_no_rebuy_when_reenter_off():
    cfg = _cfg(open_reenter=False)
    d = decide_open_pump(_snap(_sig(price=1.0), 170), _port(usdc=100, token=0, cum=100), cfg)
    assert d.action == "hold" and "already" in d.reason


def test_open_reenter_on_dip():
    cfg = _cfg(open_reenter=True)
    sig = _sig(price=1.0, rsi=30, drawdown=0.20, short_momentum=0.01, flow=0.30)
    d = decide_open_pump(_snap(sig, 120), _port(usdc=1000, token=0, cum=100), cfg)
    assert d.action == "buy" and "REENTER" in d.reason


def test_open_reenter_waits_for_dip():
    cfg = _cfg(open_reenter=True)
    sig = _sig(price=1.0, rsi=70, drawdown=0.20, short_momentum=0.01, flow=0.30)  # not oversold
    d = decide_open_pump(_snap(sig, 120), _port(usdc=1000, token=0, cum=100), cfg)
    assert d.action == "hold" and "waiting for dip" in d.reason


def test_open_missed_window():
    d = decide_open_pump(_snap(_sig(price=1.0), 100), _port(usdc=100, token=0, cum=0), _cfg())
    assert d.action == "hold" and "missed open" in d.reason


def test_open_pump_target():
    d = decide_open_pump(_snap(_sig(price=3.0), 120), _port(0, 50, avg_entry=1.0, peak=3.0), _cfg())
    assert d.action == "sell" and "PUMP-TARGET" in d.reason


def test_open_stop_failed_pump():
    d = decide_open_pump(_snap(_sig(price=0.87), 120), _port(0, 50, avg_entry=1.0, peak=1.0), _cfg())
    assert d.action == "sell" and "OPEN-STOP" in d.reason


def test_open_pump_trailing():
    d = decide_open_pump(_snap(_sig(price=1.08), 120), _port(0, 50, avg_entry=1.0, peak=1.3), _cfg())
    assert d.action == "sell" and "PUMP-TRAIL" in d.reason


def test_open_dissolution():
    d = decide_open_pump(_snap(_sig(price=0.5), 10), _port(0, 50, avg_entry=1.0, peak=1.0), _cfg())
    assert d.action == "sell" and "DISSOLUTION" in d.reason


def test_open_riding_holds():
    # gr=160 is still in the early window (> open_exit_by_gr), so it rides.
    d = decide_open_pump(_snap(_sig(price=1.05), 160), _port(0, 50, avg_entry=1.0, peak=1.05), _cfg())
    assert d.action == "hold" and "riding" in d.reason


def test_open_early_exit_backstop():
    # Past the early window with no target/stop/trail trigger -> bail before the bleed.
    d = decide_open_pump(_snap(_sig(price=1.05), 119), _port(0, 50, avg_entry=1.0, peak=1.05), _cfg())
    assert d.action == "sell" and "EARLY-EXIT" in d.reason


def test_open_hold_time_exit():
    # Held past open_hold_seconds (still early in the battle) -> fast time exit.
    d = decide_open_pump(_snap(_sig(price=1.05), 160, sip=13), _port(0, 50, avg_entry=1.0, peak=1.05), _cfg())
    assert d.action == "sell" and "HOLD-TIME" in d.reason


# ── open_patient variant (no hard stop; trailing-only; smaller size) ─────────
def test_patient_holds_through_dip_no_stop():
    cfg = _cfg(strategy="open_patient")  # -30% would stop open_pump out; patient holds
    d = decide_open_pump(_snap(_sig(price=0.70), 120, sip=30), _port(0, 50, avg_entry=1.0, peak=1.0), cfg)
    assert d.action == "hold" and "riding" in d.reason


def test_patient_trailing_still_exits():
    cfg = _cfg(strategy="open_patient")
    d = decide_open_pump(_snap(_sig(price=1.13), 120), _port(0, 50, avg_entry=1.0, peak=1.30), cfg)
    assert d.action == "sell" and "PUMP-TRAIL" in d.reason


def test_patient_smaller_size():
    cfg = _cfg(strategy="open_patient")
    d = decide_open_pump(_snap(_sig(price=1.0), 178), _port(usdc=1000, token=0, cum=0), cfg)
    assert d.action == "buy" and abs(d.amount_usdc - cfg.open_patient_buy_usdc) < 1e-9


def test_dispatch_open_patient():
    d = decide(_snap(_sig(price=1.0), 178), _port(100, 0, cum=0), _cfg(strategy="open_patient"))
    assert "OPEN-PATIENT" in d.reason


# ── dispatcher ───────────────────────────────────────────────────────────────
def test_default_strategy_is_open_pump():
    assert Config().strategy == "open_pump"


def test_dispatch_selects_strategy():
    open_d = decide(_snap(_sig(price=1.0), 178), _port(100, 0, cum=0), _cfg(strategy="open_pump"))
    assert "OPEN" in open_d.reason
    rev_sig = _sig(rsi=30, drawdown=0.20, bounce=0.02, short_momentum=0.01, flow=0.30, price=90.0)
    rev_d = decide(_snap(rev_sig, 120), _port(1000, 0), _cfg(strategy="reversal"))
    assert "REVERSAL" in rev_d.reason


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
