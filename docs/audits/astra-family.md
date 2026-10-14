# Astra-Family Audit (3 repos) → for gold-ripper merge

Context: gold-ripper = Python XAUUSD/Bitget perp bot, 4 strategy modules, exit ladder
(TP1 / BE / ATR-trail / time-stop), risk rails (+$20 per 4h block target, -$3 block loss cap,
3 attempts/block, $1 risk/trade), Bitget ccxt broker (attached SL/TP + clientOid idempotency),
journal + reconcile + commands channel, drift self-check, paper broker with stress fills.

Audited:
1. `upload/repos/Astra/Astra-master/` (~3.5k lines)
2. `upload/repos/Astra-algorithmic-trader/Astra-algorithmic-trader-master/` (~2k lines)
3. `upload/repos/astra-quant-agent/astra-quant-agent-main/` (742 py files + Vue frontend + ~1000 test files)

---

## REPO 1 — Astra/Astra-master  ❌ NOT A TRADING BOT (Roblox item trader)

**What it is:** Windows-native automation for **Roblox limited-item trading** (not financial
markets). Python 3, ~600-line outboundChecker, 335-line supervisor, a Discord licensing bot,
plus **closed-source companion binaries** (`olympian.exe`, `outboundChecker.exe` are spawned,
the repo ships only the launcher logic). Monetized via hardware-locked whitelist (sha256 of
Windows UUID + buyer ID fetched from a web endpoint) and Shoppy.gg payment polling.

- `src/main.py:33` — polls `rolimons.com/itemapi/itemdetails`; `src/values` is a 1,973-line list
  of Roblox item values/RAP.
- `src/main.py:240-273` — the "strategy": refresh values file every `Value_Check_Cooldown` (min
  180s), **kill & relaunch the closed-source binary when any value changed** — a config
  hot-reload pattern, nothing more.
- `src/outboundChecker.py:198-359` — scans outbound trade offers vs RAP/value loss thresholds,
  posts to a Discord webhook. Reads Roblox `.ROBLOSECURITY` cookie from INI/env.
- `discordBot/Astra_DiscordBOT.py:438-460` — a thread watchdog: monitor thread heartbeat,
  restart on death; `:42-77` lock + timestamped backup before JSON writes; `:257` cooldown
  enforcement (5-day whitelist regen).

**Profitable logic:** none (Roblox RAP arbitrage; core binary is closed source).
**Exchange integration:** none. **Risk controls:** none (loss tolerance % on toy trades).
**Agent/autonomy:** a decent little "licensing SaaS daemon" (payment poll → JSON DB → Discord
verify → whitelist deploy → role assign) but irrelevant to gold-ripper.
**Speed:** N/A. **Avoid:** everything, incl. plaintext `.ROBLOSECURITY` cookie in INI and
bare `except:`-everywhere style. Only reusable micro-patterns: watchdog-restart-thread and
backup-before-atomic-write (gold-ripper already has both).

**VERDICT: 95% junk for this project. Skip.**

---

## REPO 2 — Astra-algorithmic-trader  ⚠️ Educational, early-stage, buggy (Indian equities, 5Paisa)

**What it is:** Python algo-trading skeleton for **NSE stocks via 5Paisa** broker (py5paisa
SDK), PostgreSQL candle cache, 3 toy strategies (SMA20/50 cross, ADX+RSI, DCA-range), a small
bar-replay backtester, virtual-trading stub, tests that place real dummy orders. Well-organized
(broker/strategy/database template classes + exceptions + logger) but clearly never finished:
several call paths crash if executed.

**Does it trade?** The live path is wired to 5Paisa (`lib/modules/broker/five_paisa.py:127-154`
place_order with RMSResponseCode check; `:195-219` websocket market-feed subscribe) but the
strategy run loops are broken:
- `lib/strategies/adx_rsi.py:152-160` and `towards_sma.py:85-97` do
  `call, price = self.run_logic(scrip)` — but `run_logic` returns a single `TradeCall`, not a
  tuple → immediate `TypeError` if ever run. `towards_sma.py:91` strftime uses `%H:%M:S`
  (`%S` typo) → crash.
- `run_backtest` in `towards_sma.py:99-124` merely records BUY/SELL markers; no P&L.

**Profitable logic (small, but real):**
1. **Fixed-step trailing-stop ratchet in the backtester** — `lib/backtesting/backtester.py:79-95`:
   for a long, `if price <= stoploss: exit`; `elif price >= stoploss + trailing_stoploss:
   stoploss += trailing_stoploss` (and mirrored for shorts). Stop only ratchets in fixed
   increments of the initial `trailing_stoploss` (0.5% of entry). Crude but honest: the exit
   comes *only* from stop/trail (no TP), so winners are pure runners — a good sanity contrast
   to gold-ripper's ladder.
2. **Signal objects carry their own risk** — `lib/strategies/template.py:9-15`: `TradeCall`
   bundles `call_type/order_type/price/stoploss/trailing_stoploss`; entries and exits stay
   attached to the signal from birth. gold-ripper already does probe-exact entry-relative stops;
   this is the same idea in object form.
3. **Instrument master daily refresh** — `five_paisa.py:80-99`: re-download scrip master only if
   file mtime ≠ today; everything maps names↔codes through one table (`convert_value:101-125`).
   Bitget/ccxt equivalent: cache `markets` daily instead of per-boot.
4. **DB-first candle cache with API fallback** — `lib/modules/broker/manager.py:14-52`: query
   Postgres first, fetch broker only on miss, persist; incremental append filters rows newer
   than the stored last timestamp (`database_manager.py:88-103`).
5. **Per-strategy performance ledger stub** — `lib/strategies/manager.py:6-21` (`Strat`):
   successful/unsuccessful executions, total_profit/loss, profit_per_execution, transactions/day
   — a per-module scorecard gold-ripper could mirror per strategy-module.

**Backtest methodology:** single-position bar replay (`backtester.py:108-149`): first 100 bars
warmup, feed candles one-by-one, limit-entry fill check (`price <= call.price` for BUY, filled
at the **candle close**, slightly conservative), stop/trail managed candle-by-candle, exits
recorded to CSV + matplotlib/plotly plots (`results.py`). No fees, no slippage, no position
sizing, no multi-symbol — it's a classroom tool.

**Things to AVOID (bugs/anti-patterns):**
- **Unsatisfiable exit condition** — `lib/strategies/dca.py:75-76`: SELL requires
  `close >= 30d-high*(1-3%)` **AND** `close*1.09 < entry_price` (price 9% *below* entry while
  simultaneously at a 30-day high) → practically never fires. Exactly the class of
  "condition looks plausible, never triggers" bug gold-ripper's no-zero-trade deadlock testing
  exists to catch.
- **Inverted indicator logic** — `adx_rsi.py:109-110`: BUY when `adx<25 AND pdi>ndi AND
  rsi>70` (buys overbought in a trendless market) and SELL when `adx>25 AND pdi<ndi AND
  rsi<30`. Momentum/mean-reversion roles are scrambled; never validated.
- **O(n²) indicator recompute** — `adx_rsi.py:94-103` recomputes ADX+RSI over the *entire*
  growing DataFrame on every candle.
- **SQL injection** — `database_manager.py:68-73,117,140` build SQL with f-strings.
- **Crash-on-error accounting** — `dump_candles_helper:104-107` assigns to a list as if dict
  (`data['datetime'] = ...`) → TypeError; bare `except` swallows login failures.

**VERDICT: ~80% junk. Steal at most the trailing-ratchet idea and the daily-refresh instrument
cache; use its bugs as a checklist.**

---

## REPO 3 — astra-quant-agent (AstraQuant)  ✅ THE REAL PRIZE — production-hardened OKX LLM-agent trader

**What it is:** AGPL-licensed autonomous crypto-perp trading system (v8.6.1): OKX-native
execution only, 15-minute decision cycle, **multi-LLM council proposes → deterministic Python
risk layer vetoes → OKX cloud OCO executes**. FastAPI backend + Vue 3 workstation + a gateway
daemon (supervisor/worker/scheduler). Chinese-audited codebase: comments cite dated production
incidents ("2026-09-30 实测…") and each fix is pinned by a test (~1000 test files, tiered
suites). This is real engineering with real post-mortems, not AI slop — the docstrings
themselves document past live-money bugs and their exact symptoms. Paper (demo) mode is the
fail-closed default; live is physically blocked until creds validated.

Architecture that matters: `scripts/ai_factor_trader.py` (1,414 lines, the quant cycle) +
`scripts/ai_brain_trader.py` (922, the LLM brain) decomposed into `scripts/trader/*` (34
modules), `scripts/factors/*` (7-tier factor engine), `scripts/brain/*` (prompt/decisions/
dispatch), `astra_backend/execution/*` (pure risk gates), `astra_backend/llm/*` (provider
failover, key rotation, caching), `astra_gateway/*` (job scheduler + supervisor + heartbeat).

### 3.1 PROFITABLE LOGIC worth stealing

**A. Continuous multi-factor scoring with named setups** — `scripts/trader/signals.py:45-334`
(`evaluate_asset_signal`, score −5..+5, entry threshold 2.2):
- **Fail-closed on missing indicators** (`signals.py:73-80`): if `rsi/ema9/ema21/ema55` are
  None → HOLD. Docstring explains the old bug: `f.get("ema9", px)` made missing EMA == price,
  so `ema9>ema21>ema55` degraded to `px>px>px` and `f.get("rsi",50)` fell into the long RSI
  band. *"缺失必须显式，不得满足任何入场条件"* — directly applicable to gold-ripper's
  indicator module (a NaN ATR must never satisfy an entry).
- Sub-factors with caps: trend ±1.5 (`regime=="BULL_TREND" and ema21_slope>0.02 → +1.2, +0.3
  extra if 1h structure HH_HL`, L116-123), volume/MACD-accel ±1.5 (MACD hist & accel same sign
  ±0.6; OBV flow ±0.5; vol_ratio≥1.25 with candle direction ±0.4, L128-142), mean-reversion
  ±1.2 (VWAP bias ≤−0.75 AND RSI≤35 → +1.2, L148-155), sentiment ±0.8, and a 7-tier resonance
  block ±1.5 (MACD hist+accel ±0.6; **hist>0 but accel<0 → −0.3 "anti-FOMO" decay penalty**,
  L199-208; MACD/RSI divergence ±0.5 one-vote-reversal L212-215; **CVD AND taker ratio must
  agree** ±0.4 L217-221; OBI ±0.3 only if `depth_reliable` L227-233; OI/price quadrant ±0.3
  L244-247).
- **Volatility/liquidity shock filter** (`signals.py:263-267`): `atr_pct ≥ 4.0 OR spread ≥ 15
  bps` → `is_high_jerk_shock` → **all breakout/pullback setups blocked** (only extreme
  mean-reversion allowed). Cheap and very portable to XAUUSD.
- **6 named institutional setups** (`signals.py:269-309`) with exact geometry, e.g. Setup 1
  institutional pullback: `regime==BULL_TREND and (ema55*0.994 ≤ px ≤ ema21*1.008) and
  38≤rsi≤56 and (bull candle or lower_wick_ratio≥0.20)` → score forced ≥2.4. Wick-confirmation
  (`lower_wick_ratio≥0.20/0.28`) is a nice trend-continuation filter gold-ripper could adopt.
- **Chop guard** (`signals.py:321-332`): ADX<20 → `is_weak_chop`; above-threshold score does
  NOT enter unless a named extreme-reversal setup fired ("震荡市防绞肉" — no grinding in chop).

**B. Composite 7-tier alpha scorer for LLM context** — `scripts/factors/scoring.py:1-80+`
(`score_composite_alpha`): per-item caps (trend ±15, momentum ±25, derivatives ±20, orderflow
±15, value-area ±12, depth ±10), explicit thresholds (ADX_TREND 22 vs ADX_SIGNAL 20 —
deliberately *not* merged, L47-51), **multiplicative damping** `*=0.8` when 1H ATR%≥4.0 or
spread>15bps (L73-78), and **directional funding-crowding block**: extreme long crowding
blocks only `BUY_LONG`, never the counter-side squeeze trade (L80+). Three stated invariants
(missing≠neutral — skip the item, don't score 0; symmetric long/short; zero imports = pure
function) are exactly the discipline gold-ripper's 4 modules want.

**C. R:R geometry + dynamic expected-value gate** — `scripts/order_risk.py:16-89`
(`validate_quote_geometry_and_rr`): NaN/Inf/≤0 rejected; long needs `sl < entry < tp`;
`ABS_MIN_RR = 1.2` (fee/slippage floor, L67-69); default floor `MIN_RISK_REWARD_RATIO=2.0`;
**high-confidence EV pass**: if `confidence ≥ 80`, allow RR≥1.2 iff
`E = P·RR − (1−P)·1.0 ≥ +0.30R` (L73-78). Deterministic, stateless, testable — gold-ripper
could gate its $1-risk entries behind the same EV formula using its own empirical WR instead
of AI confidence.

**D. Scale-out engine (TP1 leg + BE move + OCO swap)** — `scripts/trader/scale_out.py:39-309`
+ `scripts/trader/tp1.py`:
- **TP1 frozen at entry** (`tp1.py:60-77` `freeze_tp1`: idempotent — *"重算等于偷偷改单"*),
  because recomputing with the current bar's ATR makes the software TP1 and the exchange TP1
  drift apart (they once double-closed half a position).
- `tp1_geometry_ok` (`tp1.py:80-92`): refuse to arm the leg unless `spot < TP1 < TP2` (long) —
  a leg on the wrong side either gets 51000-rejected or **instantly fills = open-and-close-bleed**.
- Execution: reduceOnly market close of 35% (L187-209, `cl_ord_id="SO"+ts` for ledger
  attribution), **cancel ALL old cloud algo orders first** (L226-239) to eliminate residual
  full-size stop-loss (which previously caused reverse-opens), then re-arm remaining size OCO,
  **BE stop = entry ± 0.25% cushion** (L242-243), `scale_count=999` mutual lock vs pyramiding
  (L259). Degrades to full trailing if position < 2×minSz (L163-168).
- Post-leg-fill reconciliation path (`scale_out.py:110-140, 350-458`): if position size
  dropped to `entry_sz*(1-ratio)`, the exchange leg already fired → software only does
  BE-move + ledger + event (never market-closes again).
- Event stream `data/scale_out_events.jsonl` append-only (L317-347) — "the ledger only shows
  the final result; this stream proves scale-out actually happened."

**E. The ladder-calibration lesson (biggest direct P&L lesson for gold-ripper)** —
`scripts/risk_constants.py:100-113`: `SCALE_OUT_TRIGGER_ATR` must be the **same magnitude as
the SL ATR mult**. Old default TP1 at 1.2×ATR vs 2.0×ATR stop locked only 0.6R at TP1 →
*"赢小输大"* structure, measured **18 trades/day avg win 23.87U vs avg loss 48.01U**. Fixed to
TP1 2.0×ATR (≈1R vs the 2.0×ATR stop) + `MAX_TAKE_PROFIT_ATR=3.5` smooth-clamp on runaway TPs.
gold-ripper's TP1=1R is safe *only* against its actual stop distance — this repo's test
(`tests/trading/test_risk_reward_structure.py`) is a permanent guard worth copying.

**F. Pyramiding gates** — `scripts/risk_constants.py:92-98`: max 1 scale-in, only if base
position ≥ +0.8% profit AND AI confidence ≥75; disabled forever after any scale-out
(`scale_count=999`).

**G. Backtest engine** — `scripts/backtest_engine.py:1-120` + `scripts/backtest/{lifecycle,metrics}.py`:
portfolio backtest (6 instruments), fees taker 0.05%/maker 0.02%, slippage 0.02%, Sharpe/
Sortino/Calmar/PF/maxDD, R-multiple per trade, gatekeeper-filtered attribution count.
Limitation to note: candles come from live OKX public API (small history) → shallow samples;
its value is the metrics/reporting shape, not the data depth. gold-ripper's 26y tape is far
stronger.

### 3.2 EXCHANGE INTEGRATION gold (OKX, maps well to Bitget/ccxt)

- **Order-submit gauntlet** — `scripts/trader/order_submit.py:133-461` (`submit_protected_limit_order`),
  the only function that actually places orders, in order:
  1. route + **atomic budget reservation** (`route_and_reserve_signal`, L166-176) — reserve
     risk budget before the order exists; confirm on success, release on ANY rejection
     (every early-return path releases, L198/268/332/347/357/376/436/449/457);
  2. contract-listing reconciliation, fail-open on catalog fetch, **fail-closed on delisted**
     (L191-201);
  3. demo-sandbox 5% divergence rescale of px/tp/sl (L218-241);
  4. **market-order re-anchor**: if `ASTRA_ORDER_MODE=market`, TP/SL are rescaled to the live
     price so R:R is preserved vs the *real* fill — docstring records a live ADA short whose
     planned TP ended up on the wrong side of the fill ("开-秒平放血"), and missing spot price
     = **reject the order** (L257-277);
  5. TP width smooth-clamp with confidence-conditional min_rr (L286-311);
  6. final non-bypassable geometry+R:R validation (L318-333);
  7. **price-cross hallucination guard** (L341-358): BUY limit > spot×(1+0.5%) → reject
     ("将即时成交于意外价且 SL 锚点失真"); SELL limit < spot×(1−0.5%) → reject; |px−spot|/spot
     > 50% → reject as hallucination. Pullback limits far from price are legal (4-min
     exchange-side timeout cancel兜底).
  8. venue entry gate (same-direction exposure cap) — **fail-closed if the gate itself can't
     load** (L31-130: *"闸门没上线时放行等于不设防"*);
  9. leverage set best-effort (never blocks the protected legs, L379-396);
  10. **size derived from margin at the venue boundary** (L402-440): intent is money; qty =
      `margin*leverage/(ctVal*px)` computed once, floor to minSz, reject if below exchange
      minimum. Comment documents the old bug where gates clamped margin but OKX still sent
      the pre-clamp contract count.
  11. exchange must return a verifiable `ordId` or the submission is treated as failed (L451-458).
- **Raw OKX REST client** — `scripts/okx_rest.py` (zero-dependency, HMAC, broker tag,
  param validation `:127-206`, `place_order:281` with `attach_tp/attach_sl` = native attached
  brackets, `place_algo_oco:505`, `amend_algo_sl:572`, `pending_algo_orders:597`,
  positions/bills/positions_history). Pairs naturally with gold-ripper's ccxt Bitget broker
  (clientOid ↔ clOrdId, attached SL/TP ↔ attach_tp/sl).
- **Market data 3-level failover + pooling** — `scripts/market_data_service.py:100-186`:
  persistent Keep-Alive `requests.Session`, primary→fallback OKX hosts, then local math
  fallback for indicators (`_local_math_indicators:236`); bulk tickers to dodge per-symbol rate
  limits (`fetch_tickers_bulk:195`).
- **Pool-level TTL caches** — `scripts/factors/okx_quant_factors.py:142-227`: loan-quota /
  opt-summary / instruments are pool-level; caching them per-cycle cut 8 symbols × repeats to
  one call (OKX public limit 40req/2s per IP), with a throttle for Rubik endpoints and
  explicit `available=False` degradation marks instead of fake numbers.
- **Pending-order lifecycle** — `scripts/ai_factor_trader.py:509-585`: `clean_stale_open_orders`,
  `record_open_intent` (dedup window), `reconcile_pending_orders` (match exchange states to
  local intents, return set of live ordIds), stale-maker cancel with keep-set. Mirrors
  gold-ripper's maker-limit pending flow + reconcile.
- **Unit discipline (ctVal incident)** — `scale_out.py:350-388`: OKX position records *don't
  carry* `ctVal`; an `or 1.0` fallback silently inflated ETH PnL 10× (+24.33→+243.28U) and
  deflated ARB 10×. Fix: caller passes contract multiplier computed once ("持仓级覆盖 >
  合约池"); if undeterminable → **loud warning + continue with 1.0** (state machine must keep
  protecting money even when reporting degrades). For gold-ripper: Bitget contract
  multiplier/lotSz must come from `markets`, never silently defaulted in money math.

### 3.3 RISK CONTROLS (the strongest section of the whole family)

- **Single source of truth** — `scripts/risk_constants.py` (215 lines): every hard threshold is
  an env-overridable constant with the exact same names in engine, execution layer, admin UI
  schema, and **LLM prompt interpolation** ("提示词口径 == 代码口径", L10-11). Includes a
  module-alias trick (L206-215) so `import risk_constants` and `import scripts.risk_constants`
  are the same module object (two names = two .env reads = drift).
  Key constants: max concurrent/same-direction positions (4), single-asset margin
  min(600U, 30% equity), leverage clamp [2,5] with **cross-guard min>max → clamp to safer**
  (L61-74), risk/trade min(2% equity, caps), daily loss = **min(150U, 5% equity)**,
  stop-cooldown 30min, time-stop 4h with **0.15×ATR stagnation band** (L85-88: only time-stops
  a position whose floating P&L never left the ATR band — gold-ripper's time-stop could adopt
  this "did it go anywhere?" condition), TP1 trigger 2.0×ATR, max TP 3.5×ATR.
- **`min(absolute cap, equity ratio)` helpers** — `risk_constants.py:181-203`
  (`effective_daily_loss_limit/effective_single_asset_margin/effective_risk_per_trade`) with an
  audit note that the prompt builder previously showed equity-ratio numbers while the engine
  enforced absolute caps (2.49× mismatch!) — the fix forces one shared function.
- **Three pre-send gates** — `astra_backend/execution/risk_gates.py:54-192`:
  `clamp_leverage` (0/None config = *don't clamp*, never clamp-to-zero, L75-78),
  `clamp_margin` (min over caps that are `finite and >0` only — 0 means "unavailable",
  L104-119), `check_total_exposure` (same-direction notional; **breach = REJECT, never
  silently shrink** — *"敞口超限意味着不该再开"*, L184-191). Contains the autopsy of a dead
  gate: caller passed `"BUY_LONG"`, rows had `"buy"`, comparison never matched → gate ran for
  months doing nothing (L159-177). Lesson: normalize enums at the boundary + test with live
  shapes.
- **Unified circuit breaker** — `astra_backend/execution/circuit_breaker.py:272-362`
  (`is_circuit_breaker_active`):
  1. **Black-swan sentinel**: BTC 15m candle `close/open ≤ −3%` or `(low−open)/open ≤ −4%`
     → global halt on new entries; feed failure/unparseable/corrupt news cache → halt
     (**"不可判定=不放松"** — undeterminable = fail-closed, L272-307);
  2. breaker state file (corrupt ⇒ halt, L316-325);
  3. daily realized loss vs `effective_daily_loss_limit`, computed from the merged ledger with
     an **environment axis** (demo/live rows never mixed; unlabeled old rows counted
     conservatively, `ledger_daily_closed_pnl:149-170`);
  4. **ledger-sync sidecar freshness**: if the sync status file is stale (>2700s) or corrupt,
     daily-loss sum is "not knowable complete" ⇒ halt, and a **throttled auto-heal thread**
     re-runs the sync (120s min interval, L41-64) instead of failing forever.
- **Stop cooldown, fail-closed** — `astra_backend/execution/cooldowns.py:57-161`: missing
  cooldown file = no cooldown; **corrupt file = still-in-cooldown for everything** (L101-103);
  write path refuses to merge into a corrupt state to preserve the scene (L143-147); entries
  carry an environment axis so demo cooldowns can't block live (or vice versa, L107-112);
  ledger write failure = warn only (cloud SL still covers). gold-ripper's post-stop cooldown
  should copy this exact semantics (esp. corrupt≠missing).
- **Fail-closed dashboards** — `"0 trades" ≠ safe`; e.g. `circuit_guard` in trader and this
  module are structural twins and both treat every read error as halt.
- **Portfolio budget reservation** — `scripts/trader/reservation_reconcile.py` +
  `ai_factor_trader.py:700-810`: atomic reserve/confirm/release of margin budget per signal
  with reconcile vs real positions (prevents over-commit while orders are in flight).
- **Evolution shield (anti-poisoning)** — `scripts/evolution_shield.py:1-60` +
  `scripts/evolution/lesson_health.py`: self-learned "lessons" must pass red-lines (never
  "never go long/short", never widen stops to bag-hold, no martingale/revenge sizing); lessons
  get a **health score from the win-rate/expectancy of their applicable cohort** (symbol/
  direction/exit-mechanism), regressed toward 50% by sample size, auto-archived (never deleted)
  below threshold. The docstring honestly notes health-scoring was promised in docs but never
  implemented for a year until forced. This is a principled version of gold-ripper's drift
  monitor: per-module performance-weighted enable/disable **with anti-single-event bias**.

### 3.4 AGENT/AUTONOMY (decide→execute→journal, fully autonomous)

- **The 15-min cycle**: `astra_gateway/scheduler.py` (`JobSpec("trader", …, 15*60)`) drives a
  supervised worker (`astra_gateway/supervisor.py:ensure_worker` pidfile+lock, `worker.py`
  heartbeat file + job history pruning). Jobs = trader cycle, ledger sync, backups, market-data
  health, nightly maintenance — a real unattended-ops loop (Prometheus metrics + Grafana in
  `deploy/observability/`, QQ notifier as the Telegram analog).
- **Quant stage first, LLM second, code always vetoes**: factor library (ThreadPool over 8
  symbols, `factor_library.py:44-60`) → `evaluate_asset_signal` (+ composite alpha score) →
  `scripts/brain/packages.py` renders a full-market prompt (with risk-budget lines built from
  the SAME `risk_constants`) → LLM council returns JSON decisions →
  `scripts/brain/decisions.py:41-101` **physical validation** (only 4 checks: action lexicon,
  data_quality=="valid" or WAIT, no reverse-position conflict, geometry+R:R single-source) →
  `assemble_decision_cache` (:104-260) writes a versioned, policy-hash-bound decision cache
  with **three-state observability** (`decision_source: model|omitted`,
  `gate_blocked`, `gate_reason`, `model_reason`) so dashboards can tell "model waited" from
  "code blocked" — they measured 28% of slots were model-omissions that previously looked
  identical to WAIT (L133-138).
- **Council** — `astra_backend/council_manager.py:1-80`: 3 peer LLM trader seats (conservative
  trend-pullback / aggressive momentum-breakout / quant microstructure) that can also manage
  open positions (HOLD/CLOSE_MARKET/UPDATE_SL trail) and pending orders (CANCEL stale vs
  KEEP), then a CIO seat arbitrates which plan is funded. Hard-won ops numbers in comments:
  60s timeout budget → **0 successes in 50 cycles** (silent degradation), raised to 240s with
  `CIO_MIN_ARBITRATION_TIME`; timeout budget is single-sourced (`council/policy.py`) because
  three copies disagreed.
- **Prompt caching honesty** — `astra_gateway/cache_warmer.py:1-40`: they measured that prompt
  prewarming **cost more than it saved** (cache lifetime ~minutes vs 15-min cycles; only ~14%
  of input was the stable head) and defaulted it **off**, keeping a telemetry-instrumented
  `jit` experiment. A model example of "measure, don't cargo-cult".
- **Self-evolution loop** — `scripts/self_improvement_engine.py` + `scripts/evolution/*`:
  periodic review of closed-trade evidence → asset multipliers (clamped **[0.5, 1.5]**,
  `decisions.py:167-171`) and strategy weights (clamped **[0.7, 1.3]**, `signals.py:315`),
  cooldown pool for assets with poor WR (checked *before* scoring, `signals.py:68-70`), lesson
  store with health decay. All changes are clamped, observable, and reversible — autonomy
  without letting the model rewrite its own risk rails.
- **Disaster recovery & honesty docs** — `RECOVERY_GUIDE.md` (emergency liquidation playbook),
  `docs/FAILURE_SEMANTICS.md` (post-mortems), policy snapshot/hash binding on every decision
  (`policy_snapshot.py`) so any historical decision can be tied to the exact rules that
  produced it. gold-ripper's journal could store a config hash per trade the same way.

### 3.5 SPEED

- ThreadPool factor computation per symbol; **pool-level TTL caches** for shared OKX endpoints
  (`okx_quant_factors.py:142-227`) — the big win vs naive per-symbol fetching.
- Persistent Keep-Alive sessions + dual-host failover (`market_data_service.py:100-186`).
- LLM prompt prefix caching architecture (>90% static doctrine) — but measured, and warmer
  defaulted OFF when ROI was negative (`cache_warmer.py`).
- Atomic writes everywhere are mkstemp+fsync+replace (`circuit_breaker.py:231-248`).
- Note the anti-pattern they dodged: Astra-algo-trader's per-candle full-DataFrame recompute;
  AstraQuant computes once per 15-min cycle over fixed windows.

### 3.6 THINGS TO AVOID (from their own incident log)

- Never let missing data equal neutral/price (signals.py L73-80), never default contract
  multipliers (ctVal → 1.0 incident, scale_out.py L350-388).
- "BUY_LONG" vs "buy" enum mismatch killed an exposure gate for months (risk_gates.py L159-177).
- TP1 recomputed per cycle drifted from the exchange-leg price (tp1.py L1-21) → freeze at entry.
- Notification duplicates with contradictory numbers (estimate path vs ledger path) →
  **single money-truth source**: only the exchange-fill path publishes PnL (scale_out.py
  L298-307); the 15-min path publishes actions only.
- Env-key hygiene: every env var read must exist in `env.example` (enforced by
  `tests/audit/test_env_keys_are_documented.py`); test runs must never write production `data/`
  (they had fixture rows polluting the real scale-out stream, scale_out.py L313-320).
- Backtest = shallow live-API candles → don't trust its absolute numbers; use it only as a
  reporting harness.
- The council/prompts are Chinese-first; porting seat prompts means re-writing, not copying.

**VERDICT: the only repo in the family with genuine production-grade trading engineering.
Steal aggressively — patterns, constants, and failure semantics, not the OKX specifics.**

---

## Cross-repo summary table

| Area | Astra (Roblox) | Astra-algo-trader | AstraQuant |
|---|---|---|---|
| Real trading | ❌ Roblox items | ⚠️ NSE/5Paisa, broken loops | ✅ OKX perps, live-tested |
| Signal quality | none | toy, partly inverted | strong multi-factor + 6 setups |
| Risk rails | none | SL on signal only | deep: breakers, cooldowns, EV gate, reservation |
| Exchange craft | none | naive | excellent (gauntlet, re-anchor, unit discipline) |
| Autonomy | licensing daemon | stub | full cycle + council + self-evolution w/ shields |
| Verdict | skip | 2 small ideas | top steal source |
