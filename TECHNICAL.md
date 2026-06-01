# Technical deep-dive

Companion to the [README](README.md). This documents the **verified** Battleground Alpha protocol
(reverse-engineered from `/self-hosted.md` + live API calls), the signal/decision engine, and the
operational hardening. Everything here was confirmed against the live platform, not assumed.

---

## 1. The real protocol (verified)

- **API base:** `https://alpha.creator.bid/api`
- **Chain:** id `42069`. `/api/game` advertises `rpcUrl = http://alpha.creator.bid:8545`, but that
  host is **Cloudflare-fronted and does not serve JSON-RPC**. The live node is the raw IP
  `http://5.161.35.78:8545` — the agent **probes candidates at startup and uses the first that
  responds** (`main._pick_working_rpc`).
- **Contracts (public, identical for every agent):** Factory `0xE841…220e7`, base USDC
  `0xed38…6cf4`, shared Trader `0x521F…3540`, Roles key `0xfaca…1941`. All amounts are 18 decimals.

### Endpoints used

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/game` | status, `tradingOpen`, `gameRemaining`, current token, chain info |
| `POST` | `/auth/login` | `{code}` → user JWT  ·  `{address,signature}` → agent JWT (SIWE) |
| `POST` | `/auth/nonce` | `{address}` → `{message}` to sign |
| `POST` | `/agents/register` | (Bearer user JWT) provisions Trading/Treasury Safes + airdrop |
| `POST` | `/agents/heartbeat` · `/agents/refill` | liveness · top-up funds |
| `POST` | `/tokens/:addr/swap/signature` | (Bearer agent JWT) EIP-712 sig + `sqrtPriceLimit` |
| `GET`  | `/tokens/:addr/trades?limit=N` | recent tick-level swaps (the data feed) |

### Lifecycle & rules

`lobby (60s) → marketmaking (60s, swarm only) → live (180s, public) → ended`. Trade only when
`tradingOpen`. The Trading Safe is funded with your dashboard **Buy-in per game (1–1000 USDC)**;
the EOA holds ~0.5 ETH for gas. One agent per account.

### On-chain trade path

The platform **signer** returns the swap's EIP-712 signature; the agent only relays it — it never
constructs swap cryptography itself (low risk). Execution:

```
roles.execTransactionWithRole(
    TRADER, 0, calldata, operation=1 /*delegatecall*/, ROLE_KEY, true)
where calldata = Trader.tradeViaFactory(FACTORY,
    {signature, data, expiresAt, nonce}, {sqrtPriceLimit, minAmountOut: 0}, 0)
```

The signature response is **nested** (`{signature: {signature, data, expiresAt, nonce}, sqrtPriceLimit}`)
— a subtlety that breaks naive integrations; `chain._unpack_sig` normalizes both nested and flat shapes.

---

## 2. Signal engine (`strategy.py`, pure & unit-tested)

All signals derive from the tick-level swap feed:

| Signal | Definition |
|---|---|
| `rsi` | classic RSI over `rsi_period` price changes (short = fast) |
| `drawdown` | `(high − low) / high` over the structure window — the liquidity sweep |
| `bounce` | `(price − low) / low` — recovery off the local bottom |
| `short_momentum` | momentum over a fast window — is price turning up *now* |
| `flow_imbalance` | `(buy_usdc − sell_usdc) / (buy_usdc + sell_usdc)` ∈ [−1,1] |
| `vwap` / `volatility` | volume-weighted price / stdev of per-trade returns |

## 3. `decide()` — the decision tree

**Holding** (checked every tick vs real cost basis, in priority order):
1. **Dissolution backstop** — `gameRemaining < EXIT_SECONDS` → flatten (fires *even with no cost
   basis*; this regression once stranded a bag to zero and is now covered by a test).
2. **Stop-loss** — `unrealized ≤ −STOP_LOSS_PCT`.
3. **Take-profit** — `unrealized ≥ +TAKE_PROFIT_PCT`.
4. **Trailing stop** — once peak gain ≥ `TRAIL_ACTIVATE_PCT`, exit on a `TRAILING_STOP_PCT` pullback.

**Flat** — enter only if ALL hold: `drawdown ≥ MIN_DRAWDOWN`, `short_momentum ≥ MIN_ENTRY_MOMENTUM`,
`rsi ≤ RSI_BUY_MAX`, `flow ≥ FLOW_MIN`, take-profit beats round-trip cost, and a composite score
(oversold + flow + drawdown) ≥ `CONFIDENCE_THRESHOLD`. Re-enters on each fresh setup; an 8s
post-stop-loss cooldown prevents re-buying a continuing crash.

---

## 4. Operational hardening

| Concern | Solution |
|---|---|
| Unreachable advertised RPC | Probe candidates, use the live node, pin via `BID_RPC_URL` |
| 24h agent JWT expiry | SIWE re-mint before expiry / on 401 |
| Doomed trades wasting gas | `estimate_gas` pre-flight; skip if it reverts; +50% gas headroom |
| Nonce collisions | Single in-flight trade, serialized sends behind a lock |
| Crashes / transient errors | Supervisor re-enters the loop; auto-refill on empty |
| Cost-basis race (balance lag) | Cost basis mutated only by trade workers + fresh-battle, never by the polling loop |

---

## 5. What we changed, and why (data-driven iteration)

- **v1 → v2:** added profit-taking, stop-loss, trailing stop, and cost-aware entries. v1 only exited
  at dissolution, so every favorable move round-tripped to breakeven.
- **v2 → v3:** replaced "buy below VWAP" (which caught falling knives) with **ICT-style reversal
  entries** (sweep + RSI-oversold + turn-up + buyers), faster loop, multiple entries/battle, and a
  post-loss cooldown. Cut per-battle losses ~4×.
- **Bug fixes from live logs:** the dissolution-without-cost-basis strand, and the balance-read race
  that wiped cost basis mid-hold — both fixed and regression-tested.

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `First run needs credentials` | set `BID_ACCESS_CODE` or `BID_USER_JWT` in `.env` |
| `WinError 10051` / `connected=False` | RPC unreachable — agent auto-falls back to `5.161.35.78:8545` |
| `swap rejected (403): market-making` | normal in the first 60s; loop waits for `tradingOpen` |
| `pre-flight revert, skipping` | expected on a fast pool — trade wasn't viable that instant, retries |
| `409 already registered` + 0 balance | old wallet/chain — delete `.agent.json` for a fresh EOA |
