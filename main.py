"""Battleground Alpha self-hosted trading agent — entry point.

Lifecycle:
  bootstrap  -> wallet load/create, login-with-code, one-time register (+airdrop),
                resolve chain config from /api/game, build the on-chain trader.
  heartbeat  -> background thread pings /agents/heartbeat every <=60s.
  main loop  -> poll /api/game ~1Hz; only trade when tradingOpen; on a fresh
                battle reset state + approve the token; each tick read balances +
                trades, build signals, call decide(), execute, enforce the buy
                cap; flatten before dissolution; log per-battle PnL.
  supervise  -> any uncaught error restarts the loop instead of killing the agent.

Run:  python main.py     (set BID_ACCESS_CODE + BID_DRY_RUN in .env first)
"""
from __future__ import annotations

import os
import signal
import threading
import time
import traceback

import requests

from bid_client import BIDAPIError, BIDClient
from chain import ChainTrader
from config import DEFAULT_RPC_URL, ConfigError, load_config
from strategy import Portfolio, Snapshot, build_signals, decide
from utils import (
    from_wei18, jwt_expiring, load_or_create_account, now_ts, safe_div,
    setup_logging, short_addr, to_wei18, write_state,
)


def _pick(d: dict, *keys, default=None):
    """Return the first present key (tolerates snake_case / camelCase)."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _privkey_hex(account) -> str:
    raw = account.key.hex()
    return raw if raw.startswith("0x") else "0x" + raw


class TradingAgent:
    def __init__(self):
        self.cfg = load_config()
        self.log = setup_logging(self.cfg)
        self.client = BIDClient(self.cfg, self.log)

        self.account = None
        self.state = None
        self.chain = None

        self._stop = threading.Event()
        self._trade_in_flight = False

        # Per-battle state.
        self.last_token = None
        self.cumulative_buys = 0.0     # USDC bought this battle (cap tracking)
        self.pos_cost_usdc = 0.0       # USDC cost basis of the OPEN position
        self.peak_price = 0.0          # highest mark since entry (trailing stop)
        self.entry_gr = None           # gameRemaining when the current bag was opened
        self.cap_reached = False
        self.battle_start_usdc = None
        self.tick = 0
        self.total_pnl = 0.0
        self._last_observe = 0
        self._last_status = 0
        self._cooldown_until = 0       # epoch; no new entries until then (post-loss)
        self._approved_token = None    # token already pre-approved this battle

    # ── bootstrap ──────────────────────────────────────────────────────────────
    def bootstrap(self):
        cfg = self.cfg
        state_exists = os.path.exists(cfg.state_file)
        cfg.validate(state_exists)

        self.account, self.state = load_or_create_account(cfg, self.log)
        if self.state is None:
            self._register()

        self._resolve_chain_config()
        self.chain = ChainTrader(cfg, self.state, self.log, cfg.rpc_url)
        self._report_funding()

    def _register(self):
        cfg = self.cfg
        user_jwt = cfg.user_jwt
        if not user_jwt:
            self.log.info("exchanging access code for a user JWT…")
            user_jwt = self.client.login_with_code(cfg.access_code)
        name = cfg.agent_name or ("agent-" + self.account.address[2:10])

        self.log.info("registering '%s' (EOA %s)…", name, self.account.address)
        reg = self.client.register(self.account, user_jwt, name, cfg.archetype)

        trading_safe = _pick(reg, "trading_safe", "tradingSafe")
        if not trading_safe:
            raise RuntimeError(f"register did not provision Safes — response: {reg}")

        self.state = {
            "name": _pick(reg, "name", default=name),
            "pk": _privkey_hex(self.account),
            "address": self.account.address,
            "agentJwt": _pick(reg, "token", "agentJwt", "jwt"),
            "tradingSafe": trading_safe,
            "treasurySafe": _pick(reg, "treasury_safe", "treasurySafe"),
            "rolesMod": _pick(reg, "roles_modifier", "rolesModifier", "rolesMod"),
        }
        write_state(cfg.state_file, self.state)
        self.log.info("registered '%s' — Trading Safe %s", self.state["name"], trading_safe)
        funding = _pick(reg, "fundingTx", "funding_tx")
        if funding:
            self.log.info("funding tx: %s", funding)
        self.log.info("waiting %ds for airdrop to confirm…", cfg.funding_wait_s)
        self._stop.wait(cfg.funding_wait_s)

    def _resolve_chain_config(self):
        """Refresh chain/contract addresses from the live game, then pick a
        REACHABLE RPC. /api/game advertises a Cloudflare-fronted RPC host that
        does not serve JSON-RPC, so we probe candidates and use the first that
        actually responds (explicit BID_RPC_URL > default node IP > game value)."""
        chain = {}
        try:
            game = self.client.get_game()
            chain = game.get("chain", {}) if isinstance(game, dict) else {}
        except Exception as exc:
            self.log.warning("could not fetch /game for chain config (%s); using defaults", exc)

        self.cfg.chain_id = int(_pick(chain, "chainId", "chain_id", default=self.cfg.chain_id))
        self.cfg.factory = _pick(chain, "factory", default=self.cfg.factory)
        self.cfg.usdc = _pick(chain, "baseToken", "usdc", default=self.cfg.usdc)

        candidates = []
        for url in (self.cfg.rpc_url, DEFAULT_RPC_URL, _pick(chain, "rpcUrl", "rpc_url")):
            if url and url not in candidates:
                candidates.append(url)
        self.cfg.rpc_url = self._pick_working_rpc(candidates)

    def _pick_working_rpc(self, candidates):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
        for url in candidates:
            try:
                resp = requests.post(url, json=payload, timeout=5)
                data = resp.json()
                if resp.ok and isinstance(data, dict) and data.get("result"):
                    self.log.info("using RPC %s (chainId %s)", url, int(data["result"], 16))
                    return url
            except Exception as exc:
                self.log.debug("RPC %s unreachable: %s", url, exc)
        self.log.warning("no reachable RPC among %s; falling back to %s", candidates, candidates[0])
        return candidates[0]

    def _report_funding(self):
        try:
            usdc_wei, _ = self.chain.balances(None)
            eth_wei = self.chain.eth_balance()
        except Exception as exc:
            self.log.warning("could not read balances: %s", exc)
            return
        self.log.info("Trading Safe USDC=%.2f | EOA ETH=%.4f",
                      from_wei18(usdc_wei), from_wei18(eth_wei))
        if usdc_wei == 0 and self.cfg.auto_refill:
            self.log.info("Trading Safe empty — requesting refill…")
            if self.client.refill(self.state["address"]):
                self._stop.wait(4)

    # ── auth refresh ───────────────────────────────────────────────────────────
    def fresh_jwt(self) -> str:
        if jwt_expiring(self.state.get("agentJwt")):
            return self._siwe_relogin()
        return self.state["agentJwt"]

    def _siwe_relogin(self) -> str:
        token = self.client.siwe_login(self.account)
        self.state["agentJwt"] = token
        write_state(self.cfg.state_file, self.state)
        self.log.info("agent JWT refreshed")
        return token

    # ── heartbeat ──────────────────────────────────────────────────────────────
    def _heartbeat_loop(self):
        while not self._stop.is_set():
            self.client.heartbeat(self.state["address"])
            if self._stop.wait(self.cfg.heartbeat_s):
                break

    def start_heartbeat(self):
        threading.Thread(target=self._heartbeat_loop, daemon=True,
                         name="heartbeat").start()

    # ── trade execution (worker thread) ─────────────────────────────────────────
    def _run_trade(self, token_address, amount_wei, is_buy, expected_usdc):
        try:
            self._execute_trade(token_address, amount_wei, is_buy, expected_usdc)
        except Exception as exc:
            self.log.error("trade error: %s", exc)
        finally:
            self._trade_in_flight = False

    def _execute_trade(self, token_address, amount_wei, is_buy, expected_usdc):
        token = self.fresh_jwt()
        try:
            sig = self.client.get_swap_signature(token_address, amount_wei, is_buy, token)
        except BIDAPIError as exc:
            if exc.status == 401:                       # stale JWT — relogin once
                token = self._siwe_relogin()
                sig = self.client.get_swap_signature(token_address, amount_wei, is_buy, token)
            elif exc.status == 403:
                msg = (exc.message or "").lower()
                if "cap" in msg:
                    self.cap_reached = True
                    self.log.warning("per-battle buy cap reached — pausing buys this battle")
                else:
                    self.log.info("swap rejected (403): %s", exc.message)
                return
            else:
                raise

        self.chain.trade(token_address, amount_wei, is_buy, sig)
        if is_buy:
            self.cumulative_buys += expected_usdc
            self.pos_cost_usdc += expected_usdc      # cost basis of the open bag
            self.log.info("cumulative buys this battle: %.2f / %.0f USDC",
                          self.cumulative_buys, self.cfg.buy_cap_usdc)
        else:
            # Bag sold -> position closed. Reset cost basis + peak + entry clock
            # HERE (worker, after the sell confirms) instead of in the loop, to
            # avoid the balance-read race that once wiped the cost basis mid-hold.
            self.pos_cost_usdc = 0.0
            self.peak_price = 0.0
            self.entry_gr = None

    # ── per-battle bookkeeping ───────────────────────────────────────────────────
    def _prepare_battle(self, token_address):
        """Reset per-battle state and PRE-APPROVE the token. Runs the moment a new
        token appears — usually during market-making, BEFORE live opens — so the
        open buy can fire instantly when trading opens (no approve in the hot path)."""
        if self.last_token is not None:
            self._log_battle_pnl()
        self.cumulative_buys = 0.0
        self.pos_cost_usdc = 0.0
        self.peak_price = 0.0
        self.entry_gr = None
        self.cap_reached = False
        self.tick = 0
        self._approved_token = None
        self.log.info("── new battle: token %s (pre-approving) ──", token_address)
        self._dispatch_approve(token_address)

        try:
            usdc_wei, _ = self.chain.balances(None)
            if usdc_wei == 0 and self.cfg.auto_refill:
                self.client.refill(self.state["address"])
                self._stop.wait(3)
                usdc_wei, _ = self.chain.balances(None)
            self.battle_start_usdc = from_wei18(usdc_wei)
        except Exception as exc:
            self.log.debug("battle start balance read failed: %s", exc)
            self.battle_start_usdc = None

    def _dispatch_approve(self, token_address):
        """Approve the battle token off the hot path (background thread)."""
        if self.cfg.dry_run:
            self.log.info("[DRY_RUN] would pre-approve %s", token_address)
            self._approved_token = token_address
            return

        def _approve():
            try:
                self.chain.approve_factory(token_address)
                self._approved_token = token_address
            except Exception as exc:
                self.log.warning("approve failed (sells may not settle): %s", exc)

        threading.Thread(target=_approve, daemon=True, name="approve").start()

    def _log_battle_pnl(self):
        if self.battle_start_usdc is None:
            return
        try:
            usdc_wei, _ = self.chain.balances(None)
        except Exception:
            return
        end_usdc = from_wei18(usdc_wei)
        pnl = end_usdc - self.battle_start_usdc
        self.total_pnl += pnl
        self.log.info("battle PnL: %+.2f USDC (%.2f -> %.2f) | total %+.2f",
                      pnl, self.battle_start_usdc, end_usdc, self.total_pnl)

    # ── one tick of the trading loop ─────────────────────────────────────────────
    def _tick(self, game):
        token_info = game.get("token") or {}
        token_address = token_info.get("address")
        if not token_address:
            return
        current_price = token_info.get("currentPrice")
        game_remaining = game.get("gameRemaining")
        # NB: battle prep + token approval already happened in the main loop the
        # moment this token appeared (during market-making), so we trade immediately.

        try:
            usdc_wei, token_wei = self.chain.balances(token_address)
        except Exception as exc:
            self.log.debug("balance read failed this tick: %s", exc)
            return

        try:
            trades = self.client.get_trades(token_address, self.cfg.trades_limit)
        except Exception as exc:
            self.log.debug("trades fetch failed: %s", exc)
            trades = []

        signals = build_signals(trades, current_price, self.cfg)
        mark = signals.price
        usdc_f = from_wei18(usdc_wei)
        token_f = from_wei18(token_wei)

        # Position cost basis + trailing peak. pos_cost_usdc / peak_price are
        # mutated ONLY by trade workers (buy += / sell reset) and on a fresh
        # battle -- never reset here, so a lagging balanceOf read right after a
        # buy can't wipe the cost basis (which once stranded a bag into dissolution).
        if token_f > self.cfg.min_token_sell and self.pos_cost_usdc > 0:
            avg_entry = self.pos_cost_usdc / token_f
            if mark > 0:
                self.peak_price = max(self.peak_price, mark)
            if self.entry_gr is None and game_remaining is not None:
                self.entry_gr = game_remaining
        else:
            avg_entry = 0.0

        sip = (self.entry_gr - game_remaining
               if self.entry_gr is not None and game_remaining is not None else 0)
        snapshot = Snapshot(token_address, mark, signals, game_remaining, self.tick, False, sip)
        portfolio = Portfolio(usdc_f, token_f, self.cumulative_buys, avg_entry, self.peak_price)
        decision = decide(snapshot, portfolio, self.cfg, self.log)
        self.tick += 1

        # Throttled live status line (visualization).
        now = now_ts()
        if now - self._last_status >= 3:
            self._last_status = now
            pos = ("FLAT" if token_f <= self.cfg.min_token_sell
                   else "LONG %.2f@%.6f unrl=%+.3f" % (
                       token_f, avg_entry, safe_div(mark - avg_entry, avg_entry)))
            self.log.info("live gr=%s px=%.6f vwap=%.6f rsi=%.0f dd=%.3f bnc=%.3f flow=%.2f | %s | %s",
                          game_remaining, mark, signals.vwap, signals.rsi, signals.drawdown,
                          signals.bounce, signals.flow_imbalance, pos, decision.reason[:62])

        if not decision.is_trade:
            return
        if self._trade_in_flight:
            self.log.debug("decision %s held: trade already in flight", decision.action)
            return
        if decision.is_buy and self.cap_reached:
            return
        if decision.is_buy and now_ts() < self._cooldown_until:
            self.log.debug("entry suppressed: post-loss cooldown (%ds left)",
                           self._cooldown_until - now_ts())
            return

        if decision.is_buy:
            amount_wei = min(to_wei18(decision.amount_usdc), usdc_wei)
            expected_usdc = from_wei18(amount_wei)
        else:
            amount_wei = token_wei  # sell the exact on-chain bag (no float drift)
            expected_usdc = 0.0
            if "STOP-LOSS" in decision.reason or "TRAILING" in decision.reason:
                self._cooldown_until = now_ts() + self.cfg.loss_cooldown_s
        if amount_wei <= 0:
            return

        self.log.info("DECISION %s %.6f %s — %s", decision.action.upper(),
                      from_wei18(amount_wei), "USDC" if decision.is_buy else "tokens",
                      decision.reason)
        self._trade_in_flight = True
        threading.Thread(
            target=self._run_trade,
            args=(token_address, amount_wei, decision.is_buy, expected_usdc),
            daemon=True, name="trade",
        ).start()

    def _observe(self, game):
        """Log liveness during non-trading phases (lobby / market-making)."""
        if now_ts() - self._last_observe < 10:
            return
        self._last_observe = now_ts()
        token = (game.get("token") or {}).get("symbol", "?")
        self.log.info("observing — phase=%s token=%s gameRemaining=%s mmRemaining=%s",
                      game.get("status"), token, game.get("gameRemaining"),
                      game.get("mmRemaining"))

    def _idle_sleep(self, game) -> float:
        """How long to sleep while not trading. As the live open approaches we
        fast-poll so we catch tradingOpen within ~open_poll_s and buy the open."""
        if game.get("status") == "marketmaking":
            now, mm_end = game.get("now"), game.get("mmEndAt")
            if (now is not None and mm_end is not None
                    and 0 <= (mm_end - now) <= self.cfg.open_poll_window):
                return self.cfg.open_poll_s   # imminent open -> fast poll
            return 1.0
        return 2.0

    # ── main loop + supervisor ───────────────────────────────────────────────────
    def run(self):
        self.bootstrap()
        self.start_heartbeat()
        self.log.info("agent live: %s | DRY_RUN=%s | buy_cap=%.0f USDC | trade_size=%.0f",
                      short_addr(self.state["address"]), self.cfg.dry_run,
                      self.cfg.buy_cap_usdc, self.cfg.trade_size_usdc)

        while not self._stop.is_set():
            try:
                game = self.client.get_game()
            except Exception as exc:
                self.log.debug("game fetch failed: %s", exc)
                self._stop.wait(3)
                continue

            # Prepare each new battle the instant its token appears (usually during
            # market-making) -> pre-approve so the open buy fires with no delay.
            token = (game.get("token") or {}).get("address")
            if token and token != self.last_token:
                try:
                    self._prepare_battle(token)
                except Exception as exc:
                    self.log.warning("battle prep error: %s", exc)
                self.last_token = token

            if not game.get("tradingOpen"):
                self._observe(game)
                self._stop.wait(self._idle_sleep(game))
                continue

            try:
                self._tick(game)
            except Exception as exc:
                self.log.error("tick error: %s", exc)
            self._stop.wait(self.cfg.poll_interval_s)

        self.log.info("shutdown: stop signal received")

    def supervise(self):
        """Restart the loop on unexpected crashes (bootstrap is idempotent once
        .agent.json exists). Mirrors the platform template's supervisor."""
        while not self._stop.is_set():
            try:
                self.run()
                return
            except KeyboardInterrupt:
                self._stop.set()
                return
            except ConfigError as exc:
                self.log.error("config error: %s", exc)
                return
            except Exception as exc:
                self.log.error("loop crashed, restarting in 3s: %s", exc)
                self.log.debug("%s", traceback.format_exc())
                self._stop.wait(3)

    def shutdown(self, *_):
        self._stop.set()


def main():
    agent = TradingAgent()
    for sig_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig_name):
            try:
                signal.signal(getattr(signal, sig_name), agent.shutdown)
            except (ValueError, OSError):
                pass  # not in main thread / unsupported on this platform
    agent.supervise()


if __name__ == "__main__":
    main()
