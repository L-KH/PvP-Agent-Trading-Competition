"""Rolling battle memory — the agent 'learns' the recent pump regime from past
battles and adapts its take-profit target.

Deterministic, fast, no LLM: after every battle we log {our return, how far the
price pumped from the open}. Before each battle we read the rolling median pump
and aim the take-profit at a fraction of it — so the target tracks the regime
(tight when pumps are small, looser when they're big). Plain JSON on disk, fully
defensive: it NEVER raises into the trading loop.
"""
from __future__ import annotations

import json
from typing import List, Optional


class BattleMemory:
    def __init__(self, path: str, window: int = 30, cap: int = 400):
        self.path = path
        self.window = window
        self.cap = cap
        self.records: List[dict] = self._load()

    # ── persistence (best-effort) ────────────────────────────────────────────
    def _load(self) -> List[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data[-self.cap:] if isinstance(data, list) else []
        except Exception:
            return []

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.records[-self.cap:], fh)
        except Exception:
            pass

    def record(self, token: str, ret_pct: float, peak_gain: float) -> None:
        """ret_pct: our return for the battle (e.g. -0.31). peak_gain: how far
        the price rose from the open to its peak (e.g. 0.50 = +50%)."""
        self.records.append({"token": token, "ret": float(ret_pct),
                             "peak_gain": float(peak_gain)})
        self.records = self.records[-self.cap:]
        self._save()

    # ── learned statistics over the rolling window ───────────────────────────
    def _recent(self, key: str) -> List[float]:
        return [r[key] for r in self.records[-self.window:]
                if isinstance(r, dict) and r.get(key) is not None]

    def median_peak_gain(self, default: float) -> float:
        g = sorted(self._recent("peak_gain"))
        if not g:
            return default
        n = len(g)
        return g[n // 2] if n % 2 else (g[n // 2 - 1] + g[n // 2]) / 2.0

    def win_rate(self) -> Optional[float]:
        r = self._recent("ret")
        return (sum(1 for x in r if x > 0) / len(r)) if r else None

    def avg_return(self) -> Optional[float]:
        r = self._recent("ret")
        return (sum(r) / len(r)) if r else None

    def count(self) -> int:
        return len(self.records)
