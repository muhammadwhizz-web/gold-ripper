# Passivbot v8.2.0.dev0 — Audit for gold-ripper merge

Repo: `/home/z/my-project/upload/repos/passivbot/passivbot-master/` (~2k stars, master @ v8.2.0.dev0).
Scope read: `passivbot-rust/src/{entries,closes,risk,utils,dynamic,entry_cooldown,orchestrator,analysis,backtest}.rs`,
`passivbot-rust/src/strategies/{mod,trailing_martingale,trailing_grid_v7,ema_anchor}.rs`,
`src/passivbot.py`, `src/exchanges/{bitget,ccxt_bot}.py`, `src/live/{hsl_live,order_churn_gate,reconciler}.py`,
`src/optimize.py`, `src/optimization/{warmup,warmup_utils.py}`, `src/opt_utils.py`, `docs/{risk_management,adaptive_entry_cooldown,config.bot}.md`.

**Architecture headline**: all order-generation math (entries, closes, unstuck, TWEL enforcer, cooldown) lives in a Rust
`orchestrator` crate compiled via PyO3 (`import passivbot_rust as pbr`). The SAME code path runs live and in the backtest
(`backtest.rs` feeds candle rows into the same `orchestrator` calls). Live is just a thin async Python shell that
fetches state → builds `OrchestratorInput` → diffs output orders against open orders → cancels/creates. This is the
single most valuable structural idea: **live/backtest parity by construction**.

---

## 1. GRID / DCA ENTRY LOGIC

Three strategies (`passivbot-rust/src/strategies/mod.rs:14-22`): `trailing_martingale` (default, the v7 "pbstoch" descendant),
`ema_anchor` (new v8 MM-style), `trailing_grid_v7` (deprecated compat shim for explicit v7 migrations).

### Params (mod.rs:55-83)
```
entry:  ema_span_0, ema_span_1, double_down_factor, ema_gate_mode, initial_ema_dist, initial_qty_pct,
        threshold_base_pct, threshold_we_weight, threshold_volatility_{1h,1m}_weight,
        retracement_base_pct, retracement_we_weight, retracement_volatility_{1h,1m}_weight
close:  qty_pct, threshold_base_pct, threshold_we_weight, threshold_volatility_{1h,1m}_weight,
        retracement_base_pct, retracement_volatility_{1h,1m}_weight
```
(v7 names → v8: `entry_grid_spacing_pct → entry.threshold_base_pct`, `entry_grid_spacing_we_weight → entry.threshold_we_weight`;
see `src/config/migrations/trailing_grid_v7.py:29-31`.)

### EMA bands — "relative grid" with a geometric-mean third span
`passivbot-rust/src/orchestrator.rs:959-967`:
```rust
let ema2_span = (ema_span_0 * ema_span_1).sqrt();
let lower = ema0.min(ema1).min(ema2);
let upper = ema0.max(ema1).max(ema2);
```
Bands = min/max over {EMA(span0), EMA(span1), EMA(√(span0·span1))} of 1m closes. The geometric-mean EMA
tightens the band when the two spans are far apart.

### Initial entry
Price gate (`utils.rs:425-435`): `bid_limit = min(order_book.bid, ema_lower*(1-initial_ema_dist))` — the EMA
filter is a *price cap*, not a boolean: you can only buy `initial_ema_dist` below the lower band, and never above
the current bid. Gate modes `initial | reentry | all | disabled` (`mod.rs:26-42`).

Qty (`entries.rs:48-69`):
```rust
qty = max(min_entry_qty, round(cost_to_qty(balance * WEL * initial_qty_pct, price, c_mult)))
```
i.e. first clip = `balance * wallet_exposure_limit * initial_qty_pct` notional. Re-entry after partial fills
tops the position back up to the target initial clip when `psize < initial_qty` (`entries.rs:450-467`, order types
`EntryInitialPartialLong` etc.).

### Re-entry (grid leg)
Price (`entries.rs:349-384`):
```rust
reentry_price = min(round_dn(pprice * (1 - threshold_base_pct * spacing_multiplier)), order_book.bid)
spacing_multiplier = 1 + we_ratio*threshold_we_weight + vol1h*w_1h + vol1m*w_1m   // dynamic.rs:26-35
```
`we_ratio = wallet_exposure / effective_WEL`. Grid spacing **widens** as exposure fills and as volatility rises —
the grid is exponential-ish in practice because each new order is spaced off the *updated* average entry price.

Qty (`entries.rs:320-347`):
```rust
reentry_qty = max(psize * double_down_factor, cost_to_qty(balance, price) * WEL * initial_qty_pct)
```
Classic martingale DDF with a floor of the initial clip size.

### WEL stop & cropping
`entries.rs:479-481`: `if wallet_exposure >= effective_WEL * 0.999 { return Order::default(); }`.
Then `calc_cropped_reentry_qty` (`entries.rs:272-318`) computes `wallet_exposure_if_filled` via
`calc_new_psize_pprice` (`utils.rs:283-304`, VWAP of merged position) and, if it would exceed the limit
(`> limit*1.01`), **interpolates the qty down** so the filled exposure lands exactly on the limit:
```rust
entry_qty_abs = interpolate(limit, &[we, we_if_filled], &[psize, psize+qty]) - psize
```

### Trailing entry (the "pbstoch" pattern, `entries.rs:590-752`)
Uses a `TrailingPriceBundle {min_since_open, max_since_min, max_since_open, min_since_max}` maintained by the live
loop (`src/passivbot.py:376-385`) and updated per candle in backtests. Long trigger (`entries.rs:686-699`):
```rust
if min_since_open < pprice*(1-threshold_pct) && max_since_min > min_since_open*(1+retracement_pct) {
    entry_triggered = true;
    reentry_price = min(bid, round_dn(pprice*(1 - threshold_pct + retracement_pct))) }
```
i.e. wait for price to dip `threshold` below your average entry, then wait for a `retracement` bounce off that
dip, then buy — DCA on confirmed reversal instead of a falling knife. All four threshold/retracement weights are
dynamic on wallet-exposure and volatility via the shared multiplier.

### One order per side per cycle + adaptive cooldown
`orchestrator.rs:569-600`: after generation, `keep_only_first_add_order` keeps a **single** re-entry order per
side (unless `cooldown==0` and trailing-retracement disabled, which permits the full ladder). Entry cooldown
(`entry_cooldown.rs:115-148`):
```rust
minutes = clamp(base + exposure_ratio_weight*exposure_ratio + adverse_score*adverse_weight, min, max)
```
where `adverse_score` is the RMS unilateralness of 1m log-returns (`docs/adaptive_entry_cooldown.md`):
```
r = log(close_t) - log(close_{t-1}); mean += a*(r-mean); square += a*(r*r-square);
signed_unilateralness = mean/sqrt(square); U = |...| ∈ [0,1]   (a = 2/(span+1), span default 60)
```
After each entry-increase fill the bot waits this long before posting the next add; the wait grows with exposure
and with trend strength *against* the position. A DCA brake you cannot get from static grids.

## 2. WALLET EXPOSURE LIMIT MATH (the masterclass)

- Definition (`utils.rs:255-265`): `wallet_exposure = qty*price*c_mult / balance` — **unleveraged** balance is the
  denominator; exchange leverage is irrelevant to risk math (`docs/risk_management.md` §1).
- Per-position limit (`src/passivbot.py:10308-10325`): `WEL = total_wallet_exposure_limit / n_positions` (per side),
  overridable per coin via `coin_overrides`.
- Excess allowance (`entries.rs:23-46`): `effective = min(raw_pct, TWEL/WEL - 1)`, `effective_limit = WEL*(1+effective)`.
  Lets one hot position "borrow" headroom from idle slots but never exceed the side total.
- Filled-exposure projection (`utils.rs:267-280`) — simulate the VWAP merge, then check the limit.
- **Cross-symbol entry gate** (`risk.rs:80-310` `gate_entries_by_twel`): sum current WE across all positions; if
  TWEL already reached → no entries; else greedily *remove farthest-from-market entries* until TWEL-if-filled fits,
  then **binary-search (64 iterations) the last kept entry's qty** down to the largest step-aligned qty that still
  fits under TWEL — subject to min_qty/min_cost.
- **Per-position auto-reduce / WEL enforcer** (`closes.rs:98-266`): if `WE > WEL*threshold`, iteratively grow a
  reduce qty (rounding up by `qty_step`) until post-close WE is *strictly* below target — closes rounding/min-qty
  edge cases with a 10k-iteration safeguard.
- **Portfolio TWE enforcer** (`risk.rs:628-916`): if side total > `TWEL*threshold`, sort candidates by
  `adverse_loss_per_exposure = (-projected_pnl)/exposure` (worst loser per unit exposure first), cut each down
  toward `limit/effective_n_positions`, cascade to next position until exposure is repaired. Close prices are
  crossed 0.05% past market (`price*0.9995` for long closes) to guarantee fills.
- **Balance hysteresis** (`src/passivbot.py:14250-14265` + `utils.rs:172-188`): sizing balance only updates when it
  moved >2% (`balance_hysteresis_snap_pct`) from the last used value — prevents order churn from funding payments
  and micro-PnL.

## 3. EXIT LOGIC

### Grid close (trailing_martingale, `closes.rs:283-353`)
```rust
threshold_pct = close.threshold_base_pct + we_ratio*close.threshold_we_weight
                + vol1m*w_1m + vol1h*w_1h                              // closes.rs:283-306
close_price = max(order_book.ask, round_up(pprice*(1+threshold_pct)))
```
Take-profit markup **scales with exposure** (bigger bag → bigger required markup) and with volatility. Qty
(`closes.rs:61-96`): `close_qty = min(psize, max(min_qty, round_up(full_psize*qty_pct + leftover)))`, and if the
partial close would leave a sub-min-qty dust remainder, close the whole position instead (`closes.rs:88-95`).

### Trailing close (`closes.rs:355-464`)
Symmetric to trailing entry: wait for `max_since_open > pprice*(1+threshold)`, then for
`min_since_max < max_since_open*(1-retracement)`, close at `ask` (price ≈ `pprice*(1+threshold-retracement)`).
Locks in more profit than a static TP when a spike reverses.

### v7 secondary grid close (the famous markup ladder, `strategies/trailing_grid_v7.rs:918-1001`)
```rust
start = pprice*(1+grid_markup_start); end = pprice*(1+grid_markup_end);   // e.g. +1% .. +0.5%
n_steps = ceil(|end-start|/price_step)
close_grid_qty_pct_modified = max(grid_qty_pct, 1/n_steps)                // can't make more chunks than steps
close_price = start + (end-start)*min(1, wallet_exposure/WEL)             // exposure-full → smaller markup
```
One limit order per step of the price ladder, each sized `close_qty_pct` of the position; the *richer the wallet
exposure, the closer to breakeven the exit sits*. (Qty clamped `≥ 1/n_steps` so the ladder always sums to full.)

### Auto-unstuck (`risk.rs:336-612`)
Trigger: `WE/effective_WEL > unstuck_threshold` (`risk.rs:415`). Optional **EMA gate** (`risk.rs:429-471`): a
stuck long may only be reduced when `price ≥ ema_upper*(1+unstuck_ema_dist)` — chip away at local highs, never
dump into the bottom. Selection: **least stuck first** (smallest `|pprice-price|/pprice` diff, `risk.rs:476-499`).
Chunk: `qty = balance*WEL*unstuck_close_pct/price` (`risk.rs:538-543`). Loss pacing (`utils.rs:412-423`):
```rust
balance_peak = balance + (pnl_cumsum_max - pnl_cumsum_last);
allowance = balance_peak * (loss_allowance_pct + (balance/balance_peak - 1)).max(0)
```
if the chunk's realized loss would exceed the allowance, **scale the qty proportionally** (`risk.rs:551-556`) —
it takes small, affordable losses instead of a stop-loss event.

### Realized-loss floor (`orchestrator.rs:1698-1769`)
`balance_floor = balance_peak*(1 - live.max_realized_loss_pct)`; any lossy close that would push simulated balance
below the floor is blocked (panic closes excepted). Complementary to unstuck pacing: one is per-order budget, the
other a hard account floor.

### Reducer consolidation (`orchestrator.rs:660-734`, `docs/risk_management.md`)
Only ONE protective reducer per position survives (panic > TWEL > WEL-enforcer > unstuck); ordinary closes are
capped to remaining position size; competing intents resolved "largest loss-admissible reduction wins". Prevents
the classic bug of protective orders stacking past position size.

### "Unfilled entries expire"
There is no TTL field: each cycle the bot re-derives ideal orders and **diffs** against open orders
(`src/live/reconciler.py`, `passivbot.py:18404-18451`); anything no longer in the plan is cancelled, entries within
tolerance are left alone (`apply_order_match_tolerance`), cancels/creates sorted by market distance and executed in
parallel (`ccxt_bot.py:1394-1431`). Stale limits die on the next planning tick — same effect as TTL expiry without
a timer, and gold-ripper's "cancel_pending" could adopt it.

## 4. PBSTOCH / OTHER MODES

- `trailing_martingale` **is** the successor of the old `pbstoch` mode (threshold/retracement trailing entries and
  closes, §1/§3 above). No stochastics anymore — the "stoch" role is now played by the trailing extrema bundle.
- **`ema_anchor`** (new v8, `strategies/ema_anchor.rs:39-179`) — worth reading as a *context-following MM*:
  ```rust
  inventory_shift = signed_WE_ratio * offset_psize_weight            // ±pips with position
  bid = min(order_book.bid, ema_lower*(1 - offset*vol_mult - inventory_shift))
  ask = max(order_book.ask, ema_upper*(1 + offset*vol_mult - inventory_shift))
  qty  = base_qty * max(1, 1 + same_side_WE_ratio*entry_double_down_factor)
  ```
  Quotes both sides around the EMA band, skews quotes INTO inventory (leans with the trend instead of fading it),
  and scales size with exposure. This is the closest thing in the repo to a momentum/breakout mode.
- **Forager** (coin selection, `src/config/bot.py:594-634`, `docs/forager.md`): per side, prune by quote-volume drop,
  rank by weighted `score = w_volume*volume_z + w_ema*ema_readiness + w_vol*volatility + w_uni*unilateralness`
  (default `volatility=1.0`), fill up to `n_positions`, with score hysteresis to avoid flip-flopping symbols.
- **HSL (equity hard stop loss)** — separate Rust module (`passivbot-rust/src/hsl.rs`) flattening everything when a
  windowed equity drawdown breaches a floor; see `docs/equity_hard_stop_loss.md`. Conceptually gold-ripper's -$3
  rail, formalized with warmup/cooldown contracts.

## 5. OPTIMIZATION

- **Engine**: multi-objective evolutionary. DEAP backend: `tools.selNSGA2` + ParetoFront hall-of-fame
  (`src/optimization/backends/deap_backend.py:170-330`); pymoo backend: NSGA-II/NSGA-III with SBX crossover
  (eta 20) + polynomial mutation (`pymoo_backend.py:14-21,50-63`, default pop 250/500). The old harmony-search is
  gone; it's NSGA-II mu+lambda streaming (`optimize.py:1193`).
- **Objectives**: user picks `optimize.scoring` specs (e.g. `adg_weighted`, `sharpe_ratio`, `drawdown_worst`);
  fitness = engine value per objective **plus** hard-limit violations as +1e6 penalties
  (`optimize.py:2005-2103`, `build_limit_checks` penalty_weight=1e6). Limits like `"drawdown_worst > 0.35"` are
  constraints, not objective terms — candidates violating them are dominated out.
- **Warmup / anti-overfit** (`src/warmup_utils.py:385-487`, `src/optimization/warmup.py:382-432`):
  `warmup_minutes = max(all indicator spans, searched-bound maxima) * warmup_ratio`, capped by
  `max_warmup_minutes`; per-coin `trade_start_index = first_valid_idx + warmup_minutes`
  (`optimization/warmup.py:421-431`) — backtests *never trade during warmup*, and the warmup window itself is part
  of the searched parameter space's worst case (bounds high ends are scanned so a long EMA span can't secretly
  leak warmup data).
- **Dead-parameter hygiene** (`optimization/warmup.py:78-147`): if `close.retracement_base_pct==0`, all
  retracement weights are pinned to 0 and excluded from search — no wasted dimensions, no optimizer games.
- **Anchored fine-tune** (`optimization/fine_tune_anchors.py`): seed the search from prior best configs, freeze
  selected keys, re-optimize the rest (`apply_fine_tune_bounds`, `optimize.py:3071-3149`); plus `--polish-pct`
  bound shrinking for endgame refinement. Starting-config selection tournament (`starting_config_selection.py`).
- **Metrics** (`analysis.rs:1723-1880+`): `adg` (smoothed terminal geometric daily gain), `sharpe/sortino/omega`
  from daily equity pct-changes, `equity_choppiness = Σ|Δeq| / |net_gain|`, `equity_jerkiness` = normalized mean
  |2nd derivative|, `exponential_fit_error` = MSE of log-linear fit, `peak_recovery_hours`, `liquidation` flag.
- **GPU screening** (`src/optimization/gpu/`, Metal/CUDA kernels) does float32 coarse screening; exact Rust
  re-validation decides accepted results — fast search without trusting fast math.

## 6. EXCHANGE LAYER — BITGET (`src/exchanges/bitget.py`)

- **Account-mode autodetect** (`bitget.py:69-100`): probe v3 `private_uta_get_v3_account_assets`; classic error
  codes {40084, 25245} → classic v2/mix path; inconclusive errors **fail loudly** (never misroute a UTA key).
- **Broker code** (`bitget.py:56-67`): `client.options["broker"] = self.broker_code` — rebate attribution on every
  session (gold-ripper should set its own channel code).
- **Min cost floor** (`bitget.py:171-177`): `self.min_costs[symbol] = max(5.1, limits.cost.min)` — Bitget's real
  5.1 USDT floor, not the advertised one.
- **Position-side semantics** (`bitget.py:104-223`): `posSide/holdSide/tradeSide` normalization; hedge vs one-way
  ("net") detection; `tradeSide open/close` is authoritative for reduce-only when `reduceOnly` is absent
  (`_derive_one_way_position_side`, `bitget.py:149-161`).
- **Balance** (`bitget.py:262-289`): UTA `usdtEquity - usdtUnrealisedPnl` (Bitget's effEquity is *discounted
  margin value*, not balance); classic union mode `unionTotalMargin - unrealizedPL`.
- **Order idempotency / self-recognition** (`passivbot.py:322-370` + `ccxt_bot.py:1365-1392`): every create carries
  `clientOrderId` containing a `0x<hex4>` order-type id (`type_token`); fills are decoded back via
  `custom_id_to_snake` so a fill on the wire is attributed to its exact bot role. Bitget adds an order_id→clientOid
  cache primed from the fill-events file (`bitget.py:298-388`) because Bitget fills don't echo clientOid.
- **Fill events pagination** (`bitget.py:389-540`): walk `fill_history` windows backwards with dedupe by
  fillId/execId, hard cap 200 fetches, 1-day buffer steps; UTA v3 uses cursor pagination + 30-day window cap.
- **Parallel execution** (`ccxt_bot.py:1394-1431`): `asyncio.gather` over creates and over cancels with failure
  collection via `_handle_order_write_failures`.
- **WS**: `ccxt.watch_orders()` loop with reconnect/backoff and REST fallback (`ccxt_bot.py:659-737`); sparse WS
  fills flag `_pb_order_update_requires_authoritative_refresh` → a REST confirm before the plan trusts them.
- **Churn gate** (`src/live/order_churn_gate.py`): tracks per-cohort (symbol/side/reduce_only/type) cancel-replace
  patterns with deterministic one-to-one matching + continuous-drift detection; blocks executions that would
  evidence churn (protects rate limits from a flapping plan).

## 7. SPEED

- **Rust core**: all per-symbol order math compiled (PyO3); EMA/volatility values cached per (symbol, span) in the
  orchestrator EMA bundle; backtest runs candle-by-candle at millions of steps/s; live shares it.
- **Plan→verify→execute** (`src/live/hsl_live.py:595-623`): account facts (positions/open orders/balance
  signatures) are captured before planning and re-checked after; if anything moved, the plan is discarded — no
  locks, no stale-order trades, still fast.
- **Optimistic freshness ledger** (`src/live/freshness.py`, `_record_authoritative_surface`): only *changed*
  surfaces trigger downstream recomputes; unchanged snapshots are no-ops.
- **Candle manager** (`src/candlestick_manager.py`) caches 1m OHLCV on disk with staggered concurrent warmup
  (`warmup_candles_staggered`, `passivbot.py:4674+`) and per-exchange fetch concurrency caps
  (`_candle_fetch_concurrency`, `passivbot.py:6106-6126`).
- **Balance/qty hysteresis + match tolerance** (§2, §3) kill the two biggest sources of wasted REST calls
  (thrash re-derivation).

## 8. THINGS TO AVOID — grid failure modes & how passivbot mitigates them

1. **Grids blow up in trends.** Mitigations, in order of importance: (a) hard WEL stop at 0.999×limit +
   qty-cropping to the limit (`entries.rs:479`, `272-318`); (b) cross-symbol TWEL gate (`risk.rs:80`); (c) EMA
   price-cap on initial entries (`utils.rs:425`); (d) spacing that widens with WE and volatility (§1); (e)
   adaptive cooldown with adverse-directionality weight (§1); (f) unstuck pacing with loss allowance (§3).
2. **Never a naked stop-out**: passivbot replaces stop-losses with paced partial closes + account floor
   (`max_realized_loss_pct`) + optional HSL. A single catastrophic close is the last resort (panic), and only one
   reducer survives per position.
3. **Min-qty/min-cost traps**: every qty is floored at `calc_min_entry_qty` and the close ladder checks that a
   partial close doesn't strand dust (`closes.rs:88-95`); Bitget's real 5.1 USDT min cost is hardcoded.
4. **Rounding chaos**: `tolerant_round_dn/up_preserve_step` snaps float noise only when within
   `f64::EPSILON*4` of a step boundary (`utils.rs:49-70`) — otherwise gold-ripper's grid will drift off-grid.
5. **Allowance vs min-qty contract** (`docs/risk_management.md`): an unstuck close snapped up to exchange minimum
   can exceed the loss allowance; the bot then waits for profits to rebuild it. Design your rails for this.
6. **Union margin balance** (Bitget): `available` ≠ wallet; use `unionTotalMargin - unrealizedPL` or you will
   systematically mis-size positions.
7. **Don't let the plan churn**: hysteresis-snapped balance (2%), order match tolerance, and a churn gate are why
   passivbot can run dozens of symbols without burning rate limits.

## TOP STEALABLE ITEMS (ranked)

1. **WE-cropped re-entry qty** (`entries.rs:272-318`) — every DCA add interpolated so filled exposure lands exactly
   on the cap; kills the "#N+1 order over-leverages" bug class.
2. **Dynamic distance multiplier** (`dynamic.rs:26-35`) — one 9-line function: spacing & markups scale with
   `1 + we_ratio*w + vol_1h*w + vol_1m*w`. Drop-in for gold-ripper's fixed ATR multiples.
3. **Trailing threshold/retracement entries** (`entries.rs:590-752`) — DCA only after a confirmed bounce;
   `min_since_open/max_since_min` extrema bundle is ~20 lines of state.
4. **Auto-unstuck with EMA gate + allowance pacing** (`risk.rs:336-612`, `utils.rs:412-423`) — replaces "stuck bag
   hope" with a paced, budgeted deleveraging policy that only sells local highs.
5. **Exposure-weighted exit markup / secondary grid** (`trailing_grid_v7.rs:918-1001`) — bigger bag → smaller
   markup target; interpolate `markup_start→markup_end` by `WE/WEL`.
6. **Excess allowance + TWEL entry gate with binary-search qty crop** (`entries.rs:23-46`, `risk.rs:80-310`) —
   per-slot limits that borrow from idle slots without breaking the side total.
7. **TWE enforcer ranking by adverse_loss_per_exposure** (`risk.rs:628-916`) — when the rail trips, cut the worst
   loser per unit exposure first (gold-ripper's -$3 rail generalized).
8. **Balance hysteresis snap (2%)** (`passivbot.py:14250-14265`, `utils.rs:172-188`) — 3 lines, removes order
   churn from funding/micro-PnL.
9. **Adaptive entry cooldown + RMS unilateralness** (`entry_cooldown.rs:115-148`, `docs/adaptive_entry_cooldown.md`)
   — wait longer between adds when exposure high or trend adverse; U = |EMA(r)|/√EMA(r²) ∈ [0,1].
10. **Optimizer pattern** (`optimize.py:2005-2103`, backends) — NSGA-II multi-objective + hard limits as 1e6
    penalties + warmup-guarded backtests + Pareto front export; the anti-overfit recipe (warmup_ratio,
    max_warmup_minutes, dead-param pinning) is directly reusable around gold-ripper's train.py.
11. **Bitget client layer** (`bitget.py`) — UTA/classic autodetect, tradeSide/posSide normalization, 5.1 USDT
    floor, equity-upnl balance, clientOid type-embedding for fill attribution.
12. **Plan→verify→execute + one-reducer consolidation** (`hsl_live.py:595-623`, `orchestrator.rs:560-734`) —
    re-check account facts after planning, before sending; only one protective order per position ever.
