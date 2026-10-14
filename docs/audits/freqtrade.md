# Freqtrade Engineering Audit — gold for gold-ripper

Repo: `/home/z/my-project/upload/repos/freqtrade/freqtrade-develop/` (freqtrade-develop, ~2026 dev branch)
Audit scope: protections plugin system, exchange order handling (Bitget!), backtest hygiene, HTF merge without lookahead, DataProvider patterns, retrier/rate-limit, plot/analysis tooling, pairlist management.
All paths relative to repo root unless absolute. Line numbers verified against this checkout.

Context: gold-ripper is a single-symbol (XAUUSD) Bitget USDT-perp Python bot with 4 strategy modules, an exit ladder (TP1/BE/ATR-trail/time-stop), $-based risk rails (+$20/−$3 per 4h block, +$120/−$5 per day), ccxt broker, journal, drift self-check, venue reconciliation, commands channel, news guard, paper-stress fills.

---

## 1. PROTECTIONS — the direct analog of gold-ripper's risk rails

Freqtrade implements "risk rails" as a plugin list of `IProtection` handlers that query closed trades in a lookback window and create **time-boxed, side-aware pair locks**. Gold-ripper's `+$20/-$3 per 4h block / +$120/-$5 per day` rails are a hand-rolled version of exactly this; the freqtrade design is cleaner and worth porting almost verbatim.

### 1.1 The contract: `IProtection` + `ProtectionReturn` (`freqtrade/plugins/protections/iprotection.py`)

```python
# iprotection.py:17-23
@dataclass
class ProtectionReturn:
    lock: bool
    until: datetime
    reason: str | None
    lock_side: str = "*"
```

Two hook methods (`iprotection.py:104-122`): `global_stop(...)` → locks ALL pairs (`"*"`), `stop_per_pair(...)` → locks one pair. Each protection declares `has_global_stop` / `has_local_stop` booleans (`iprotection.py:26-29`).

**Window/unit handling — minutes OR candles** (`iprotection.py:39-53`). Every protection accepts either `lookback_period` (minutes) / `stop_duration` (minutes) or `lookback_period_candles` / `stop_duration_candles`, with candles multiplied by the strategy timeframe:

```python
# iprotection.py:39-53
tf_in_min = timeframe_to_minutes(config["timeframe"])
if "stop_duration_candles" in protection_config:
    self._stop_duration_candles = int(protection_config.get("stop_duration_candles", 1))
    self._stop_duration = tf_in_min * self._stop_duration_candles
elif "unlock_at" in protection_config:
    self._unlock_at = protection_config.get("unlock_at")
else:
    self._stop_duration = int(protection_config.get("stop_duration", 60))

if "lookback_period_candles" in protection_config:
    self._lookback_period_candles = int(protection_config.get("lookback_period_candles", 1))
    self._lookback_period = tf_in_min * self._lookback_period_candles
else:
    self._lookback_period_candles = None
    self._lookback_period = int(protection_config.get("lookback_period", 60))
```

Mutual exclusivity is validated up front (`protectionmanager.py:112-131`): you may not set both `stop_duration` and `stop_duration_candles`, nor both lookback variants, nor `unlock_at` with a duration.

**`unlock_at` — lock until a fixed wall-clock hour** (`iprotection.py:134-140`): parse `"HH:MM"`, anchor to the last closed trade's close date, roll forward one day if the time already passed. This is a *session-based* cooldown (e.g. "no new trades until next London open") — directly useful for a gold bot that wants to stop after a bad block until the next session.

```python
# iprotection.py:134-140
if self._unlock_at is not None:
    hour, minutes = self._unlock_at.split(":")
    unlock_at = max_date.replace(hour=int(hour), minute=int(minutes))
    if unlock_at < max_date:
        unlock_at += timedelta(days=1)
    return unlock_at
```

**Lock end is anchored to the last offending trade, not "now"** (`iprotection.py:142`): `until = max_date + timedelta(minutes=self._stop_duration)` — this avoids stacking locks when the same event is re-evaluated on later loops.

### 1.2 StoplossGuard (`freqtrade/plugins/protections/stoploss_guard.py`) — maps to the −$3/−$5 loss rails

Params (defaults): `trade_limit=10`, `only_per_pair=False` (inverted into `_disable_global_stop`), `only_per_side=False`, `required_profit=0.0` (`stoploss_guard.py:21-24`).

Exact gating logic:

```python
# stoploss_guard.py:50-74
look_back_until = date_now - timedelta(minutes=self._lookback_period)

trades1 = Trade.get_trades_proxy(pair=pair, is_open=False, close_date=look_back_until)
trades = [
    trade
    for trade in trades1
    if (
        str(trade.exit_reason)
        in (
            ExitType.TRAILING_STOP_LOSS.value,
            ExitType.STOP_LOSS.value,
            ExitType.STOPLOSS_ON_EXCHANGE.value,
            ExitType.LIQUIDATION.value,
        )
        and trade.close_profit
        and trade.close_profit < self._profit_limit
    )
]

if self._only_per_side:
    # Long or short trades only
    trades = [trade for trade in trades if trade.trade_direction == side]

if len(trades) < self._trade_limit:
    return None
```

Notes worth stealing:
- It filters by **exit reason** (any stop-ish reason incl. liquidation), not by P&L sign. `required_profit` turns it into "N stoplosses with profit < X".
- Window query semantics: `get_trades_proxy(close_date=T)` returns trades with `close_date > T` (`freqtrade/persistence/trade_model.py:1523,1543-1545`).
- `global_stop(date_now, None, side)` evaluates across all pairs unless `only_per_pair=True` (`stoploss_guard.py:89-100`); per-pair mode locks only the offending pair (`:102-111`).
- Doc example (`docs/includes/protections.md:52-58`): `lookback_period_candles: 24, trade_limit: 4, stop_duration_candles: 4`.

### 1.3 MaxDrawdown (`freqtrade/plugins/protections/max_drawdown_protection.py`) — maps to the +$120/−$5 daily drawdown rail

Params: `trade_limit=1`, `max_allowed_drawdown=0.0`, `calculation_mode="ratios"` (`max_drawdown_protection.py:23-25`).

Logic: pull all trades closed inside the window, if fewer than `trade_limit` → pass; else compute drawdown and compare (`max_drawdown_protection.py:50-96`):

```python
# max_drawdown_protection.py:50-55
look_back_until = date_now - timedelta(minutes=self._lookback_period)
trades_in_window = Trade.get_trades_proxy(is_open=False, close_date=look_back_until)
if len(trades_in_window) < self._trade_limit:
    return None
...
if drawdown > self._max_allowed_drawdown:   # :96
```

Two calculation modes (`:58-92`):
- `"equity"` — cumulative-$ drawdown: sums `close_profit_abs` of all trades closed *before* the window to reconstruct `starting_balance + profit_before_window`, then `calculate_max_drawdown(trades_df, value_col="profit_abs", starting_balance=..., relative=True)` → `relative_account_drawdown`. **This is the $-based mode that matches gold-ripper's dollar rails.**
- `"ratios"` (default) — drawdown over per-trade `close_profit` ratios (`drawdown_abs`).

The drawdown engine itself is `freqtrade/data/metrics.py:calculate_max_drawdown` (cumulative equity high-water-mark vs trough) — the same algorithm gold-ripper can reuse for its daily −$5 rail instead of ad-hoc summation.

### 1.4 CooldownPeriod (`freqtrade/plugins/protections/cooldown_period.py`)

Simplest possible: if the pair has any closed trade within `lookback_period`, lock until `last_trade.close_date + stop_duration` (`cooldown_period.py:28-53`):

```python
# cooldown_period.py:39-45
trades = Trade.get_trades_proxy(pair=pair, is_open=False, close_date=look_back_until)
if trades:
    trade = max(trades, key=lambda t: t.close_date)
    ...
    until = self.calculate_lock_end([trade])
```

Doc example: `stop_duration_candles: 2` after every exit (`docs/includes/protections.md:131-137`).

### 1.5 LowProfitPairs (`freqtrade/plugins/protections/low_profit_pairs.py`)

Sum of `close_profit` over the window's trades must be >= `required_profit`, with `trade_limit` as a minimum sample count (`low_profit_pairs.py:57-69`):

```python
# low_profit_pairs.py:57-69
trades = Trade.get_trades_proxy(pair=pair, is_open=False, close_date=look_back_until)
trades_to_consider = [...]
if not trades_to_consider or (len(trades_to_consider) < self._trade_limit):
    return None
profit = sum(trade.close_profit for trade in trades_to_consider if trade.close_profit)
if profit < self._required_profit:
```

This is *exactly* the "+$20 per 4h block" rail expressed generically: window=4h, `required_profit=+X`, `trade_limit>=1`, side-aware.

### 1.6 The lock store: `PairLocks` (`freqtrade/persistence/pairlock_middleware.py`)

The key engineering idea: **locks are a first-class persisted object with dedupe, side awareness, and candle rounding**, and the same implementation serves live (SQL) and backtest (in-memory list):

```python
# pairlock_middleware.py:54-62  (dedupe + round up to candle boundary)
lock_end_time = timeframe_to_next_date(PairLocks.timeframe, until)
existing_locks = PairLocks.get_pair_locks(pair, now, side=side)
for lock in existing_locks:
    if (lock.reason == reason
            and lock.lock_end_time_utc == lock_end_time
            and lock.side == side):
        return lock
```

- Side semantics (`:95-104`): a lock matches if `lock.side == "*"` or equals the requested side — so a "short-only" lock (from `only_per_side` protections) doesn't block longs.
- `is_pair_locked(pair, now, side)` falls back to `is_global_lock` (`:188-190`), so `"*"` pair locks block everything.
- Manager fan-out (`freqtrade/plugins/protectionmanager.py:50-94`): every handler with `has_global_stop`/`has_local_stop` runs; a lock is created only if `not PairLocks.is_global_lock(lock.until, side=...)` — i.e. **never shorten an existing lock**, only extend.

### 1.7 Where locks are enforced & triggered live (`freqtrade/freqtradebot.py`)

- Entry gate: global `"*"` lock short-circuits the whole entry pass (`freqtradebot.py:822-836`); per-pair/per-side check right before placing the entry (`:880-892`):
  ```python
  # freqtradebot.py:880-881
  if signal:
      if self.strategy.is_pair_locked(pair, candle_date=nowtime, side=signal):
  ```
  `IStrategy.is_pair_locked` (`freqtrade/strategy/interface.py:1169-1188`) rounds `candle_date` **up to the next candle** (`timeframe_to_next_date`) so a lock can't expire in the 2-second gap between candle close and data refresh — a subtle but important anti-flap detail.
- Trigger points: after any exit fills (`freqtradebot.py:2617-2618 → handle_protections`), and when a stoploss-on-exchange order is observed triggered (`:1666-1669`).
- **Auto-lock bonus**: every exit also locks the pair for one candle ("Auto lock", `freqtradebot.py:2626-2628`) to prevent immediate re-entry on a stale signal:
  ```python
  # freqtradebot.py:2626-2628
  def handle_protections(self, pair: str, side: LongShort) -> None:
      # Lock pair for one candle to prevent immediate re-entries
      self.strategy.lock_pair(pair, datetime.now(UTC), reason="Auto lock", side=side)
  ```
- Protections run inside backtesting too via `run_protections` after every simulated close (`freqtrade/optimize/backtesting.py:1325-1328, 852`).

**Gold-ripper mapping:** port `ProtectionReturn` + `PairLocks`(file-backed instead of SQL) + the 4 handlers; express the rails as (a) `MaxDrawdown` in `equity` mode with `lookback_period=4h/24h` and dollar thresholds via a small subclass that compares `drawdown_abs` in $ (the ratios mode normalizes; equity mode gives relative — for fixed-$ thresholds compare `calculate_max_drawdown(...).drawdown_abs` directly), and (b) `StoplossGuard`-style counting of stop exit-reasons per 4h block. Keep the "anchor lock end to last trade + round to candle" and "never shorten existing locks" rules.

---

## 2. EXIT ORDER TYPES & EXCHANGE INTERACTION (incl. Bitget)

### 2.1 The `_ft_has` capability table (`freqtrade/exchange/exchange.py:133-184`)

Freqtrade declares per-exchange behavior in a dict instead of `if exchange == ...` everywhere. Defaults (`exchange.py:134-149`):

```python
# exchange.py:134-149
"stoploss_on_exchange": False,
"stop_price_param": "stopLossPrice",   # request key
"stop_price_prop": "stopLossPrice",    # response key
"stoploss_order_types": {},
"stoploss_blocks_assets": True,        # SL orders block funds on most spot exchanges
"stoploss_query_requires_stop_flag": False,
"order_time_in_force": ["GTC"],
...
# Seconds after the candle close time to assume a candle is actually closed
"ohlcv_late_candle_grace_secs": 15,
```

Subclasses deep-merge `_ft_has` and `_ft_has_futures` (`exchange.py:1037-1053`) — a pattern gold-ripper can use to keep venue quirks declarative.

### 2.2 Stoploss on exchange: placement (`create_stoploss`, `exchange.py:1646-1747`)

- Order type selection from user config via `stoploss_order_types` mapping (`:1607-1616`), e.g. `{"limit": "limit", "market": "market"}` for Bitget.
- **Stop-limit price ladder** — `stoploss_on_exchange_limit_ratio` (default 0.99) keeps the limit price 1% *beyond* the trigger so a triggered limit still fills in a fast move (`exchange.py:1618-1638`):
  ```python
  # exchange.py:1620-1626
  limit_price_pct = order_types.get("stoploss_on_exchange_limit_ratio", 0.99)
  if side == "sell":
      limit_rate = stop_price * limit_price_pct
  else:
      limit_rate = stop_price * (2 - limit_price_pct)
  bad_stop_price = (stop_price < limit_rate) if side == "sell" else (stop_price > limit_rate)
  ```
- Trigger price is precision-rounded in the *safe* direction — `ROUND_DOWN` for a buy-stop (short exit), `ROUND_UP` for a sell-stop (long exit) so rounding never pushes the trigger past the intended level (`:1676-1677`):
  ```python
  round_mode = ROUND_DOWN if side == "buy" else ROUND_UP
  stop_price_norm = self.price_to_precision(pair, stop_price, rounding_mode=round_mode)
  ```
- Futures: **reduceOnly forced on the SL order** (`exchange.py:1700-1701`):
  ```python
  if self.trading_mode == TradingMode.FUTURES:
      params["reduceOnly"] = True
      if "stoploss_price_type" in order_types and "stop_price_type_field" in self._ft_has:
          price_type = self._ft_has["stop_price_type_value_mapping"][
              order_types.get("stoploss_price_type", PriceType.LAST)]
          params[self._ft_has["stop_price_type_field"]] = price_type
  ```
  (last-price vs mark-price trigger mapping — Bitget uses `triggerType: fill_price|mark_price`).
- `create_stoploss` is `@retrier(retries=0)` (`exchange.py:1646`) — **no retries on SL placement**; a failed SL is surfaced to the bot which force-exits instead of silently retrying. Deliberate safety choice.
- Generic params: `reduceOnly=True` is injected by base `_get_params` (`exchange.py:1497-1510`); `timeInForce` only set for non-GTC non-market orders (`:1506-1507`).

### 2.3 Stoploss on exchange: maintenance loop (`freqtradebot.py:1637-1789`)

- After the entry fills, if no SL exists, place one at `trade.stoploss_or_liquidation` (`freqtradebot.py:1680-1687`).
- If *all* SL orders got canceled externally → recreate (`:1700-1712`).
- Trailing/custom-SL updates on exchange are **throttled by `stoploss_on_exchange_interval` (default 60s)** and only replace the order if the new trigger is actually better, checked via `stoploss_adjust` (`:1748-1759`):
  ```python
  # freqtradebot.py:1748-1752
  if self.exchange.stoploss_adjust(stoploss_norm, order, side=trade.exit_side):
      # we check if the update is necessary
      update_beat = self.strategy.order_types.get("stoploss_on_exchange_interval", 60)
      upd_req = datetime.now(UTC) - timedelta(seconds=update_beat)
      if trade.stoploss_last_update_utc and upd_req >= trade.stoploss_last_update_utc:
  ```
- `stoploss_adjust` (`exchange.py:1594-1605`) is the one-directional check: for a long only move the SL **up** (`side == "sell" and stop_loss > float(order[price_param])`). Gold-ripper's BE/ATR-trail ladder should adopt the same ratchet semantics + update beat.
- `stoploss_blocks_assets=False` for Bitget (`bitget.py:33`) → the bot keeps managing the position while the SL rests on-exchange (on some spot venues an open SL blocks funds, so exits are skipped while it's open — `freqtradebot.py:1672-1678`).

### 2.4 Post-only

Not a first-class global feature. TIF is passed through (`exchange.py:1506-1507`) and only Kraken maps the `PO` alias (`freqtrade/exchange/kraken.py:131-133`):
```python
if time_in_force == "PO":
    params.pop("timeInForce", None)
    params["postOnly"] = True
```
Bitget advertises `"order_time_in_force": ["GTC", "FOK", "IOC", "PO"]` (`bitget.py:37`) — ccxt maps `PO` to Bitget's post-only natively, so a gold-ripper `time_in_force="PO"` entry would need the same `params.pop → postOnly` shim only if ccxt's mapping is incomplete.

### 2.5 Bitget specifics (`freqtrade/exchange/bitget.py` — a whole-class port reference)

- Capability flags (`bitget.py:29-51`): `stoploss_on_exchange: True`, `stop_price_param/prop: stopPrice` (spot) vs `stopLossPrice` (futures), `stop_price_type_field: "triggerType"` with `{"LAST": "fill_price", "MARK": "mark_price"}`, `stoploss_blocks_assets: False`, `balance_includes_unrealized_pnl: True` (ccxt "total" = accountEquity incl. uPnL — wallet math must account for this), `ohlcv_candle_limit: 200/1000` (historic vs recent — `ohlcv_candle_limit()` override `:59-80` uses `maxRecentDaysPerTimeframe`).
- One-way position mode enforced at startup (`bitget.py:161-171`): `self._api.set_position_mode(False)` inside `additional_exchange_init` (dry-run skipped). Gold-ripper should do the same assert on boot.
- Futures order params add `marginMode` (`bitget.py:196-198`).
- **Stop-order identity quirk**: Bitget stop trigger and the follow-up real order are two orders; `_convert_stop_order` (`bitget.py:82-104`) matches the follow-up by `clientOrderId == order_id` across `fetch_canceled_and_closed_orders` + `fetch_open_orders` and rewrites `id_stop`/`type="stoploss"`/`status_stop="triggered"`.
- Fetch fallback with two param generations (`bitget.py:106-137`): `{"stop": True}` for old, `{"planType": "profit_loss"}` for new futures plan orders; cancel uses `planType: "pos_loss"` with legacy fallback (`:148-159`). Every error is mapped: `ccxt.OrderNotFound/InvalidOrder` → pass, `DDoSProtection` → `DDosProtection`, `OperationFailed/ExchangeError` → `TemporaryError`, `BaseError` → `OperationalException`.
- Liquidation price formula for isolated USDT-M (`bitget.py:200-258`) with the exact exchange formula and `mm_ratio + taker_fee_rate` denominator — useful for gold-ripper's liq-distance guard:
  ```python
  # bitget.py:252-254
  return (wallet_balance - (amount * open_rate * position_direction)) / (
      amount * (mm_ratio + taker_fee_rate - position_direction))
  ```
- Delisting/pre-settlement check via `markets[pair].info.limitOpenTime` (`bitget.py:274-290`) — the crypto analog of "contract expiry" for gold futures rollover.

### 2.6 Candle finality — don't act on an unfinished candle (`exchange.py:3146-3204`)

The just-closed candle is only trusted after a **grace period** (default 15s, clamped to half a candle) or once a newer candle exists:

```python
# exchange.py:3152, 3167-3173
return min(self._ohlcv_late_candle_grace_ms, timeframe_to_msecs(timeframe) // 2)
...
if last_candle_date != (curr_candle_date - timeframe_to_msecs(timeframe)):
    return True                      # newer candle already issued
return fetch_start_ms >= (curr_candle_date + self._ohlcv_candle_grace_ms(timeframe))
```

Plus a poll governor (`_now_is_time_to_refresh`, `:3175-3204`) that keeps re-polling within the grace window but never stays silent longer than `ohlcv_max_poll_interval_secs` (30min default) for lagging long timeframes. Gold-ripper's H1/H4 loop should copy both: grace-period finality + max-poll fallback.

---

## 3. BACKTEST HYGIENE (`freqtrade/optimize/backtesting.py` + `freqtrade/strategy/interface.py`)

### 3.1 Signal shift — the canonical lookahead guard (`backtesting.py:550-567`)

Entry/exit signal columns are shifted **one full candle** before the loop, so candle N's signal can only act on candle N+1:

```python
# backtesting.py:554-567
# To avoid using data from future, we use entry/exit signals shifted
# from the previous candle
for col in HEADERS[5:]:
    tag_col = col in ("enter_tag", "exit_tag")
    if col in df_analyzed.columns:
        df_analyzed[col] = (
            df_analyzed.loc[:, col]
            .replace([nan], [0 if not tag_col else None])
            .shift(1)
        )
...
df_analyzed = df_analyzed.drop(df_analyzed.head(1).index)
```

`validate_row` documents the same mental model (`backtesting.py:1477-1479`): "Row is treated as 'current incomplete candle'. entry / exit signals are shifted by 1 to compensate for this."

### 3.2 Same-bar SL/TP ambiguity — the pessimism rules

Docs state them (`docs/backtesting.md:556-585`) and the code enforces them:

- **Evaluation sequence** when multiple exits trigger in one candle (`interface.py:1504-1520`): Exit-signal → Stoploss → ROI → Trailing-stop. Because `_check_trade_exit` returns the first producible exit (`backtesting.py:1001-1004`), stoploss beats ROI in the same candle; exit-signal wins because it's assumed to act at the candle open (`docs/backtesting.md:565`).
- **ROI measured on the candle's best price, filled at the ROI value, clamped to the candle** (`docs/backtesting.md:567-568`; code `interface.py:1443-1446` sets `current_rate_best = high` for longs, and `backtesting.py:709-712` clamps: `return min(max(close_rate, row[LOW_IDX]), row[HIGH_IDX])`).
- **ROI that comes into effect on the triggering candle exits at open** (`backtesting.py:677-685`) — you can't retroactively claim a fill at a level that only existed after the threshold changed.
- **Opening-candle ROI on red candles rejected** (`backtesting.py:687-707`, ref issue #6261): if the entry was above open and the candle closes below it, the assumed intra-candle path is impossible → `raise ValueError("Opening candle ROI on red candles.")` and the exit is dropped.
- **Stoploss fills exactly at the stop price, but the loss is booked with 2× fees** (`docs/backtesting.md:571`); "Low happens before high for stoploss, protecting capital first" (`:573`).
- **Entry-candle trailing-stop pessimism** (`backtesting.py:618-646`): if trailing arms on the entry candle, assume price ticks just enough to arm the stop then dives:
  ```python
  # backtesting.py:618-620, 636-646
  # Special case: trailing triggers within same candle as trade opened. Assume most
  # pessimistic price movement, which is moving just enough to arm stoploss and
  # immediately going down to stop price.
  ...
  # Worst case: price ticks tiny bit above open and dives down.
  stop_rate = row[OPEN_IDX] * (1 - side_1 * abs((trade.stop_loss_pct or 0.0) / leverage))
  ...
  # Limit lower-end to candle low to avoid exits below the low.
  if is_short: return min(row[HIGH_IDX], stop_rate)
  else:        return max(row[LOW_IDX], stop_rate)
  ```
- **Order fill rule**: an order fills only if its price lies within the candle (`backtesting.py:787-789`):
  ```python
  def _get_order_filled(self, rate: float, row: tuple) -> bool:
      """Rate is within candle, therefore filled"""
      return row[LOW_IDX] <= rate <= row[HIGH_IDX]
  ```
  There is **no slippage**; custom entry/exit prices are clamped into the candle (`backtesting.py:1054-1057`, `:902-907`).
- **On-exchange stoploss is disabled in backtest** because the simulation is already "perfect" (`backtesting.py:344-347`).

### 3.3 Fees & funding

- Worst-case fee policy (`backtesting.py:268-281`): if config doesn't pin a fee, backtest uses `max(fees)` across the traded pairs/ maker-taker — i.e. *taker* on the most expensive market: "Using fee … - worst case fee from exchange (lowest tier)."
- Fees are applied on both sides of every order: `fee_open=self.fee, fee_close=self.fee` (`backtesting.py:1223-1224`), order cost includes fee: `cost=amount * close_rate * (1 + self.fee)` (`backtesting.py:973`).
- Funding fees for futures are charged on the funding interval boundary using historical funding+mark data (`backtesting.py:1007-1023`, data assembled in `_load_bt_data_detail` `:416-465`).
- `timeframe-detail` (`_load_bt_data_detail`, `get_detail_data` `:1581-1606`): optional lower-TF candle stream to resolve intra-candle ambiguity — the honest way to test a TP1/BE/ATR ladder that's over-optimistic on one 4h candle. Detail rows use `searchsorted` windows (`:1592-1593`).

### 3.4 Loop order = live order (`backtesting.py:1519-1579`)

Per candle per pair: 1) manage/cancel open orders, 2) process entries, 3) fill entry orders, 4) evaluate exits (`_check_trade_exit`), 5) fill exit orders, then `run_protections` on close (`:852`, `:1325-1328`). Wallets/`max_open_trades` re-checked per candle with an explicit reset comment for detail-mode (`:1666-1668`).

---

## 4. STRATEGY INTERFACE — HTF merge without lookahead

### 4.1 `merge_informative_pair` (`freqtrade/strategy/strategy_helper.py:117-161`, prep `:16-67`)

The rule: candle `date` is the **open** time, so an H4 candle stamped 15:00 isn't closed until 19:00 — merge it only onto base candles stamped *after* its close:

```python
# strategy_helper.py:29-49
minutes_inf = timeframe_to_minutes(timeframe_inf)
minutes = timeframe_to_minutes(timeframe)
if minutes == minutes_inf:
    # No need to forwardshift if the timeframes are identical
    informative[date_merge_column] = informative[date_column]
elif minutes < minutes_inf:
    # Subtract "small" timeframe so merging is not delayed by 1 small candle
    # Detailed explanation in https://github.com/freqtrade/freqtrade/issues/4073
    if timeframe_inf == "1M":
        informative[date_merge_column] = (informative[date_column] + pd.offsets.MonthBegin(1)) - pd.to_timedelta(minutes, "m")
    else:
        informative[date_merge_column] = (informative[date_column]
                                          + pd.to_timedelta(minutes_inf, "m")
                                          - pd.to_timedelta(minutes, "m"))
else:
    raise ValueError("Tried to merge a faster timeframe to a slower timeframe...")
```

i.e. `date_merge = inf_open + inf_tf − base_tf`, so an H4 candle opens merge eligibility on the H1 candle stamped 4h later − 1h. Merge itself uses `pd.merge_ordered(..., fill_method="ffill")` (`:85-92`, 2.5× faster than separate ffill) and back-fills only from *past* informative rows when start dates misalign (`:94-107` — "while still avoiding lookahead bias - as only past data is used").

The docstring is the specification (`strategy_helper.py:127-136`): "Since dates are candle open dates, merging a 15m candle that starts at 15:00, and a 1h candle that starts at 15:00 will result in all candles to know the close at 16:00 which they should not know. Moves the date of the informative pair by 1 time interval forward."

**Gold-ripper note:** this is *shift-free* lookahead protection — no `.shift()` on indicators; the whole HTF row (including any indicator computed on it) becomes available only after the HTF candle closes. If gold-ripper resamples H1→D1/H4 with `resample().agg()` and then `shift()`, verify instead that the merge key is `HTF_open + HTF_len − base_len` (or, equivalently, merge on the HTF close timestamp and require `base_date >= htf_close`).

### 4.2 `@informative` decorator (`freqtrade/strategy/informative_decorator.py:70-134`)

Declares HTF blocks declaratively; the runner fetches via `dp.get_pair_dataframe(asset, timeframe)` (`:278`), applies the user's `populate_indicators_<tf>`, then merges with the safe helper (`:330-344`). Bonus: an `InformativeCache` (TLRU, entries expire after 2 informative candles, fingerprinted on the last candle's OHLCV `:165-170`) so live loops don't recompute D1 indicators every H1 tick — cheap win for gold-ripper's loop cost.

### 4.3 DataProvider anti-lookahead for callbacks (`freqtrade/data/dataprovider.py`)

The mechanism that makes `dp.get_pair_dataframe(...)` safe *during backtest*:

```python
# dataprovider.py:72-86
def _set_dataframe_max_index(self, pair: str, limit_index: int):
    """Limit analyzed dataframe to max specified index. Only relevant in backtesting."""
    self.__slice_index[pair] = limit_index

def _set_dataframe_max_date(self, limit_date: datetime):
    """Limit informative dataframe to max specified index."""
    self.__slice_date = limit_date
```

```python
# dataprovider.py:390-397  (get_pair_dataframe, backtest branch)
data = self.historic_ohlcv(pair=pair, timeframe=timeframe, candle_type=candle_type)
# Cut date to timeframe-specific date.
# This is necessary to prevent lookahead bias in callbacks through informative pairs.
if self.__slice_date:
    cutoff_date = timeframe_to_prev_date(timeframe, self.__slice_date)
    data = data.loc[data["date"] < cutoff_date]
```

and `get_analyzed_dataframe` slices to the current index (capped at `MAX_DATAFRAME_CANDLES`, `:416-421`). The backtest loop advances these per candle: `_set_dataframe_max_index` (`backtesting.py:1695-1697`) and `_set_dataframe_max_date(current_time_det)` (`backtesting.py:1720`).

**Gold-ripper mapping:** its drift self-check / journal replay should fetch any HTF frame through a "cutoff" accessor that floors `now` to the *previous closed* HTF candle (`timeframe_to_prev_date`) and excludes the forming candle — exactly `data.loc[data["date"] < timeframe_to_prev_date(tf, now)]`.

### 4.4 Timeframe helpers (`freqtrade/exchange/exchange_utils_timeframe.py:68-93`)

`timeframe_to_prev_date` / `timeframe_to_next_date` = floor/ceil a timestamp onto the candle grid (via `ccxt.Exchange.round_timeframe`). Two-liner worth stealing for candle-aligned scheduling in the 4h-block rails.

---

## 5. PLOT / ANALYSIS TOOLS worth copying

- **plot-dataframe** (`freqtrade/plot/plotting.py`): plotly candlestick + trades overlay driven by a strategy's `plot_config` dict (indicators/subplots declaratively, `templates/strategy_subtemplates/plot_config_full.j2`). It loads data + backtest trades (`load_trades`), trims to timerange with startup candles (`:61-76`), and marks buy/sell/exit-reason annotations. Gold-ripper's dashboard could reuse this offline renderer to eyeball the exit ladder against actual fills.
- **Backtest exit-reason analysis**: `freqtrade/optimize/analysis/base_analysis.py` groups exit reasons by enter_tag (the tables `backtesting --analysis` prints). For an exit *ladder*, per-tag exit reason stats is the single most informative report — which TP rung actually closes, how often the time-stop fires, etc.
- **Lookahead analysis bot** (`freqtrade/optimize/analysis/lookahead.py`, `lookahead_helpers.py`): automated bias detector. It runs the full backtest, then re-runs it starting *from each entry candle* with trimmed data, and compares signals/indicators (`analyze_indicators`, `lookahead.py:65-90` compares full-run vs cut-run dataframes column-by-column to name the leaking indicator). It force-disables protections, forces market orders (or allows limits explicitly) and pins stake (`lookahead_helpers.py:149-213`). A mini version of this (rerun last 10 entries with data cut at entry time, assert identical entry decision) is a perfect permanent CI test for gold-ripper's 4 strategy modules — much stronger than eyeballing resample code.
- **Recursive analysis** (`freqtrade/optimize/analysis/recursive.py`): detects indicators whose values at candle N differ when the run is truncated at N (recursive/rolling-window issues, e.g. `pandas_ta` warmup, ffill over gaps) — same idea applied to indicator stability rather than signals.
- **Metrics**: `freqtrade/data/metrics.py::calculate_max_drawdown` (returns absolute & relative drawdown + high/low indices) — reuse for the daily rail and for reporting.

## 6. RATE LIMIT / ERROR RECOVERY (`freqtrade/exchange/common.py`)

- Retry counts: `API_RETRY_COUNT = 4` (+1 original call), fetch_order gets 5 (`common.py:33-36`).
- **Quadratic backoff** (`common.py:114-121`): `return (max_retries - remaining_retries) ** 2 + 1` → 2s, 5s, 10s, 17s … after each failure.
- The `retrier` decorator (`common.py:170-199`) retries only on `TemporaryError`/`RetryableOrderError`; `DDosProtection`/`RetryableOrderError` additionally sleep the backoff. `retrier_async` mirrors it (`:124-158`) including the KuCoin-429 special case of skipping the sleep.
- **Exception ladder** — every exchange call maps ccxt exceptions to exactly four freqtrade exceptions (see `create_order`, `exchange.py:1573-1592`):
  - `ccxt.InsufficientFunds` → `InsufficientFundsError` (no retry)
  - `ccxt.InvalidOrder` → `InvalidOrderException` (no retry — bad size/price won't heal)
  - `ccxt.DDoSProtection` → `DDosProtection` (retry + backoff)
  - `ccxt.OperationFailed/ExchangeError` → `TemporaryError` (retry + backoff)
  - `ccxt.BaseError` → `OperationalException` (fatal)
- `@retrier(retries=0)` opt-out for non-idempotent calls like `create_stoploss` (`exchange.py:1646`).
- Dry-run order lifecycle with simulated limit fills lives in `exchange.fetch_dry_run_order` / `check_dry_limit_order_filled` (`exchange.py:1467-1488`) — the model for gold-ripper's paper-stress fills (check `low <= limit_price` for buys to decide fills, as backtest `_get_order_filled` does).

## 7. PAIRLIST / WHITELIST bits that still map to a single-symbol gold bot

- **StaticPairList** is the right primitive for one symbol; the interesting part is the *filter chain* concept (`freqtrade/plugins/pairlist/`): `SpreadFilter` (`SpreadFilter.py:21,62-83` — `spread = 1 - bid/ask`, reject above `max_spread_ratio`, default 0.005) is directly reusable as a "don't enter during rollover/news spread blowout" guard on XAUUSDT perp; `VolatilityFilter`, `PriceFilter` (max dev from MA), `RangeStabilityFilter` are generic candle-stat gates.
- Whitelist hygiene in the live loop: pairs with open trades are removed from the entry pass (`freqtradebot.py:810-814`) — trivial for one symbol but the same "entry pass only sees fresh slots" pattern applies.
- `Pairs that have open trades should be processed first` in the backtest generator (`backtesting.py:1634-1635`) — order-of-processing detail that keeps exit handling deterministic.

## 8. Small but sharp

- `process()` ordering (`freqtradebot.py:307-365`): reload markets → refresh candles → bot_loop_start → analyze → manage open orders (timeouts!) → exits → liquidation warnings → position adjustments → entries. Entries are *last* — exits always first. Gold-ripper's loop should keep that invariant.
- `strategy_safe_wrapper` (`freqtrade/strategy/strategy_wrapper.py`): wraps user callbacks with exception suppression + optional log — the pattern for calling strategy module hooks so one broken module can't kill the loop.
- `LocalTrade.get_trades_proxy` works identically in live (SQL) and backtest (in-memory lists) (`trade_model.py:1507-1545`) — the journal/query API can be shared by live bot and backtest if trades are plain dataclasses with a registry.
- `log_once` (LoggingMixin) to suppress duplicate warnings per candle (`mixins/logging_mixin.py`) — keeps 4h-loop logs readable.

---

## Top steal summary (ranked, for gold-ripper)

1. **Protection framework**: `ProtectionReturn`/`IProtection` + `PairLocks` (dedupe, side-aware, candle-rounded, never-shorten) + `StoplossGuard`/`MaxDrawdown(equity)`/`LowProfitPairs`/`CooldownPeriod` as declarative rail configs; enforce locks in the entry pass only (exits stay live) and auto-lock 1 candle after each exit.
2. **Bitget exchange class** (`exchange/bitget.py`): capability `_ft_has` table, plan-order fetch/cancel params (`stop:true` vs `planType:profit_loss/pos_loss`), one-way mode assert, liq-price formula, `balance_includes_unrealized_pnl`.
3. **Backtest pessimism rules**: shift-signals-by-1, SL>ROI same-bar priority, entry-candle trailing worst-case, ROI red-candle rejection, fill-only-within-candle, worst-case fee, 2× fee on SL.
4. **`merge_informative_pair` date-shift math** + `dp._set_dataframe_max_date` cutoff accessor for HTF data.
5. **`retrier` + exception ladder** with quadratic backoff and `retries=0` for SL placement.
6. **Candle finality grace period** (`_candle_is_final`, 15s clamp half-candle) + max-poll fallback.
7. **On-exchange SL ratchet**: `stoploss_adjust` one-way check + `stoploss_on_exchange_interval` throttle + stop-limit `limit_ratio` 0.99 + safe-direction price rounding.
8. **Lookahead analysis tool** (cut-run signal comparison) as a permanent drift/CI check.
9. **`unlock_at` HH:MM session lock** and `stop_duration_candles`/`lookback_period_candles` unit handling.
10. **SpreadFilter** as pre-entry spread guard; `calculate_max_drawdown` for rail accounting and reporting.
