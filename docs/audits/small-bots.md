# Audit: 3 small GitHub trading bots (for gold-ripper mining)

Auditor: Explore (Task ID 2-a) | Date: session 2-a | Scope: 100% of code files read in each repo.
Baseline: gold-ripper = Python XAUUSD/Bitget perp bot, 4 strategy modules, exit ladder (TP1/BE/ATR-trail/time-stop), risk rails +$20/-$3 per 4h block, Bitget ccxt broker (attached SL/TP + clientOid idempotency), journal, reconcile, commands channel.

Verdicts at a glance:
| Repo | Language | Real/working? | Mines anything? |
|---|---|---|---|
| bitget-trading-bot | Python (1 file, 1157 L) | YES — real Bitget v2 REST bot, trades real orders | **GOLDMINE for Bitget API integration** |
| aster-bot | Python (~494 L) | Half — places DCA buys, but strategy/risk is stubbed | Almost nothing (anti-lessons) |
| Expert-Advisor-trading-bot | MQL5 + Python MT5 helpers | NO — two fatal logic bugs; can barely ever trade | Concepts + anti-lessons only |

---

## REPO 1: bitget-trading-bot (`upload/repos/bitget-trading-bot/bitget-trading-bot-clean-main/`)

### 1. WHAT IS IT
Single-file Python bot `bitget_bot.py` v8.9 (2025-05-29, French author, actively changelogged through V8.0→V8.9 in CHANGELOG.md). Trades **Bitget USDT-M perpetuals** (default ETHUSDT, 15m) with real signed REST v2 calls; ccxt is used only to fetch swap candles. Strategy: Bollinger(34, 2.0) breakout + RSI(14) momentum band + bandwidth-volatility filter. One position at a time, exchange-side TP/SL, optional exchange-side partial TP, Telegram alerts, file logging, internet-loss watchdog. Demo mode via Bitget's paper-trading header. This is a **real bot that really trades** — the only one of the three.

Entry path: `check_signal_bb_rsi()` (bitget_bot.py:140-170) → market order via raw API (`place_market_order` :347-384) → poll real position (`wait_for_position` :846-852) → place full TP/SL plan orders (`set_full_tp_sl` :416-475) → self-heal loop re-attaches them if missing (:1013-1025).

### 2. BITGET INTEGRATION GOLD (the reason this repo matters)
- **Hand-rolled Bitget v2 signing, exactly per Bitget docs** — `pre_hash` (:268-269) `ts + METHOD + path(+query) + body`; `sign` = HMAC-SHA256→base64 (:271-272); `send_signed_request` (:274-308):
  - headers `ACCESS-KEY`, `ACCESS-SIGN`, `ACCESS-TIMESTAMP`, `ACCESS-PASSPHRASE`, `locale: en-US` (:288-296)
  - **demo mode = header `'paptrading': '1'`** (:297-298) — paper-trade against real Bitget infra
  - GET: query string is sorted and included in the signed path (:279-284); POST: body is `json.dumps(..., separators=(',',':'), sort_keys=True)` (:276) and the exact same string is sent — signature/body mismatch impossible.
- **Demo/live productType map** (:1133-1157, used at :314 etc.): `SUSDT-FUTURES` (demo) vs `USDT-FUTURES` (live); `SCOIN-FUTURES`/`COIN-FUTURES` for coin-margined. One-line swap for demo.
- **Full-position TP/SL via plan orders** — `set_full_tp_sl` (:416-475): `POST /api/v2/mix/order/place-tpsl-order` with `planType: "pos_profit" / "pos_loss"`, `triggerType: "mark_price"`, **`executePrice: "0"`** (= market execution on trigger), `delegateType: "close"`, `holdSide`, unique `clientOid` `tp_{ts}_{rand}` (:443, :451). Exchange-side protection that survives bot death — attachable *after* entry.
- **Partial TP (server-side TP1 leg)** — `place_partial_tpp_visible` (:477-509): same endpoint, `planType: "profit_plan"`, explicit `size` (fraction of position), `orderType: "market"`, mark-price trigger (:486-491). I.e. "close 85% at +2.3% on the exchange, keep runner" without the bot being alive.
- **Pending-plan reconciliation** — `detect_current_position_bitget` (:683-766) fetches `GET /api/v2/mix/order/orders-plan-pending?planType=profit_loss`, parses `entrustedList`, classifies `pos_profit`/`pos_loss`/`profit_plan` → (full_tp, full_sl, partial_tps). Main loop **re-places missing TP/SL every cycle** (:1013-1023) and uses a `just_placed_tp_sl` flag (:1007, :1025) to skip the one-cycle visibility race right after placement. This is exactly gold-ripper's reconcile concept, implemented against Bitget's actual endpoints.
- **True fill re-pricing** — after the market order, `wait_for_position` polls `/api/v2/mix/position/single-position` 10× @0.2s (:846-852) and reads `openPriceAvg` / `total` / `holdSide` (:719-721); TP/SL are computed from **actual average entry** (:926-930, :1002-1006), not the pre-order price.
- **ccxt candle fetch hardened for Bitget** — `fetch_historical` (:195-255): `options: {defaultType: "swap", defaultSubType: "linear"}` (:205-208) to avoid spot/margin endpoints, `load_markets()` before fetch, and **error-40725 retry with exponential backoff** (5 attempts, delay 1→2→4…, :212-238). 40725 is the classic Bitget ccxt candles/margin mixup.
- Symbol conversion `"ETHUSDT" → "ETH/USDT:USDT"` (:98-100); `marginCoin = symbol[-4:].upper()` (e.g. :315) — works for `XAUUSDT` too.
- Margin/leverage setup via `/api/v2/mix/account/set-margin-mode` (:310-324) and `set-leverage` (isolated → per-holdSide loop; crossed → single call) (:326-345).
- **Internet circuit breaker** (:938-954, :1099-1125): ping `GET /api/v2/spot/public/coins` (unsigned, cheap); after `MAX_RETRIES=10` × `RETRY_DELAY=30s` send ONE Telegram alert; on recovery send resume alert and reset state; while down, no further API calls are attempted.
- **Telegram error dedupe** — `send_error_once` (:811-817): identical consecutive error text is not re-sent (anti-spam).

Caveats before copying: order body uses `tradeSide: 'open'` + `reduceOnly: 'NO'` as strings (:359-363) and queries positions by `holdSide` long/short (:392-397) — that is **hedge-mode** semantics; on a one-way account Bitget rejects `tradeSide`. Gold-ripper (one-way via ccxt) must not copy that field blindly. Position probing in `detect_current_position_bitget` also hardcodes `product='USDT-FUTURES'` (:694) even when the rest of the file honors demo.

### 3. PROFITABLE LOGIC
- **Bandwidth-expansion regime gate** (:154-157, :164): `width = (upper-lower)/SMA`; `vol_threshold = MA21(width) + 1.0·STD21(width)`; entries only when `width > vol_threshold`. A clean "volatility is expanding NOW" filter — steal as a gate for gold-ripper breakout modules (parameterizable).
- **BB breakout with RSI momentum band** (:165-168): LONG when `close > upper && RSI > 40`; SHORT when `close < lower && RSI < 60`. Note: thresholds are momentum-confirmation style (RSI_HIGH=40 for longs, RSI_LOW=60 for shorts, :82-83) — NOT mean-reversion. Concept reusable; parameters unproven for gold.
- **TP1-heavy exit ladder** (:68-75): optional partial exit of **85%** at **+2.3%**, runner to **+4.1%** TP, SL **-1.5%** (long; 4.0/1.5 short). ~2.7:1 to final TP with most risk off after TP1. Gold-ripper's ladder is richer (BE/ATR-trail/time-stop), but the "TP1 takes most of the size off, server-side" pattern is worth mirroring on-exchange.
- Signal uses **last CLOSED candle** `df.iloc[-2]` everywhere (:161, :175, :613) — no repaint on the forming candle. Same convention gold-ripper already uses.

### 4. RISK CONTROLS
Thin: position size = 10% of available margin × leverage / price (:895-898) — notional, not risk-based. No daily loss cap, no kill switch, no fees/journal. SL exists only as an exchange plan order placed AFTER fill (0.2s+ unprotected window; a fill followed by a crash in that window leaves the position naked). `TEST_MODE` forces a trade without any signal (:55-56, :879). Nothing to steal except the internet breaker and error dedupe above.

### 5. SPEED
None. Polling REST every ~60s; worse, while idle it runs `live_price_preview_simple` — 3 signed REST calls every 2s for 60s (:646-681, :1090) ≈ 90 signed calls/minute of pure dashboard cosmetics. Rate-limit burn anti-pattern.

### 6. THINGS TO AVOID
- **Shuffled placeholder credentials** (:89-93): `TELEGRAM_TOKEN = "your_api_key"`, `API_KEY = "your_passphrase"`, `API_SECRET = "your_telegram_token"`, `PASSPHRASE = "your_chat_id"` — labels point at the wrong secrets; users pasting per-label will put the Telegram token into the API key slot. Dangerous template.
- Hardcoded per-symbol precision table via `SYMBOL.startswith(...)` (:106-125) instead of `market['precision']` from `load_markets` — wrong on any unlisted symbol.
- `tradeSide:'open'`/holdSide queries assume hedge mode (see caveat above).
- Entry logic duplicated in a startup block (:879-933) AND in the loop (:970-1010) — classic drift hazard.
- **Orphaned plan orders**: pending `profit_plan`/`pos_profit` orders are never canceled when a position closes (only a Telegram note is sent, :1033-1051); a stale partial-TP can fire into the NEXT same-side position.
- `set_margin_mode` + `set_leverage` re-sent before every entry (:977-978) — wasted signed calls.
- Sizing falls back `float(entry.get('available') or entry.get('isolatedMaxAvailable'))` (:890, :975) → `float(None)` crash possible in cross mode.
- Equity dashboard mislabels `available` as "Total Capital" (:600).

---

## REPO 2: aster-bot (`upload/repos/aster-bot/aster-bot-main/`)

### 1. WHAT IS IT
~494 lines Python. Targets **Aster DEX** (asterdex.com) perps via `ccxt.aster` — **not Bitget**. What actually runs: a DCA accumulator — market-buys $25 of `BTC/USDT:USDT` every 4 hours (`main.py:69-78`), forever, with no exit. `close_position()` exists (aster_bot.py:120-130) but is never called. README claims "Real, working … trading bot" — overstated: it's an infinite leveraged long accumulator.

### 2. BITGET GOLD
None. Wrong exchange. (ccxt-aster connection boilerplate — `defaultType: 'future'`, `adjustForTimeDifference: True` — is generic, aster_bot.py:26-34.)

### 3. PROFITABLE LOGIC
None. DCA-into-perp is not an edge; the only strategy code in the repo (backtest.py EMA 12/26 cross) **is not what the bot runs** — see below.

### 4. RISK CONTROLS — the interesting failure
- **The kill switch is dead code**: `check_risk_limits()` (aster_bot.py:132-137) compares `self.daily_pnl < -MAX_DAILY_LOSS_USDT(20)`, but `daily_pnl` is only mutated by `update_pnl()` (:139-143), which **nothing ever calls**. The $20/day limit can never trigger. (Gold-ripper lesson: wire the rails to actual fills/journal, not to an accumulator nobody updates — gold-ripper already does this correctly.)
- `STOP_LOSS_PCT`, `TAKE_PROFIT_PCT`, `DEFAULT_SLIPPAGE` (config.py:26-31) are **never referenced** by any code — no SL/TP is ever placed. 5x-leveraged position with zero stop.
- `DRY_RUN` guard in `place_order` (:103-105) is the one decent pattern.

### 5. SPEED
Nothing (schedule lib, 60s tick, REST polling).

### 6. THINGS TO AVOID (gold-reaper-style traps, all present)
- **Tests that test imaginary code**: `tests.py:21-34` `TestRiskCalc` verifies ATR-based sizing (`qty = risk_usd / stop_dist`) that exists nowhere in the repo. Same smell as gold-reaper's phantom ML module.
- **Fabricated README backtest**: README.md:30-41 shows a hardcoded sample output (`pnl 312.4, win_rate 0.571`) presented like a result; the backtester itself (backtest.py) simulates an **EMA-cross strategy the bot doesn't run** → backtest-live mismatch by design.
- Backtest charges the fee on the sell leg only (backtest.py:40) — entry fee free → flattered returns.
- `requirements.txt` lists only `ccxt`, `python-dotenv` — but `main.py:7` imports `schedule` → fresh install crashes on first run.
- `check_setup.py:51` uses `config` outside the `try` that defines it → NameError when config import fails (the one case the checker exists for).
- `requirements.txt` pins `ccxt>=4.0` but `ccxt.aster` exists only in recent ccxt — broken pin for the core dependency.

---

## REPO 3: Expert-Advisor-trading-bot (`upload/repos/Expert-Advisor-trading-bot/Expert-Advisor-trading-bot-main/`)

### 1. WHAT IS IT
MQL5 MT5 Expert Advisor `bot.mq5` (471 L, "v2.0 Enhanced") + 5 MetaTrader5-python helper scripts (init / connection tests / account info / test order / JSON config manager — all competent boilerplate, diagnostics only, no strategy). EA strategy: MACD bearish cross + "bearish zone" (bullish push then a 1.5×-average-body bearish M1 candle) + fractal S/R zones + "FVG" + **SELL LIMIT at resistance**; SL = resistance + 0.5×(resistance-support); TP = 2× distance-to-support (MinRiskReward 2.0, bot.mq5:283-334); SL→BE at 1:1 (:433-471); fixed-$ risk sizing; daily loss halt; spread filter; 60s cooldown. **FX-only design** (EURUSD/GBPUSD defaults).

### 2. BITGET GOLD
None (MT5). The MQL5 order plumbing (`MqlTradeRequest`, TRADE_ACTION_PENDING, IOC filling, :308-334) is MT5-specific.

### 3. PROFITABLE LOGIC
Concept-level only, and it can't run correctly (see below):
- Bearish-reversal-after-bullish-push pattern (:148-186) — a real idea (momentum exhaustion), but the implementation's candle indexing is broken (next section), so as written it fires on inverted data.
- Fractal S/R with touch-count scoring (:190-268) — symmetric indexing, so fractal detection itself survives the ordering bug; the multi-touch scoring idea is reusable in a Python port.
- R-geometry: SL at 0.5×zone beyond entry, TP at 2R (:286-288) — sensible shape; gold-ripper already has 1R-geometry stops.

### 4. RISK CONTROLS
- **Risk-based sizing with tick-value math** (:338-395): `lotSize = riskAmount / (riskDistance/tickSize × tickValue)` with guards, lot-step floor, and a **second hard clamp at 5% of equity** (:383-392). The double-clamp shape is worth stealing; the implementation has the min-lot bug below.
- **BUT**: `if(lotSize < minLot) lotSize = minLot;` (:377-378) bumps size UP to min lot, silently **exceeding RiskAmount** on wide stops — the correct behavior is to skip the trade. Same bug in the Python helper test-function.py:62.
- Daily loss limit (bot.mq5:399-412) compares `ACCOUNT_BALANCE` to `dailyStartBalance` set once in `OnInit` (:33): **never resets at midnight**, ignores floating losses (balance≠equity), and latches `tradingEnabled=false` permanently until manual restart.
- Spread filter: `maxSpread = 0.0003` hardcoded (:416-429, "3 pips for EURUSD/GBPUSD") — on XAUUSD the spread (~0.10-0.35 price units) is always above 0.0003 → **this EA can never trade gold**. Another quietly-unsatisfiable gate, gold-reaper style.
- `PositionsTotal() > 0` guard (:75) blocks entries if ANY position exists on the account (any symbol, any magic) — deadlocks on multi-EA accounts.

### 5. SPEED
N/A (tick-driven MT5; nothing exotic).

### 6. THINGS TO AVOID — two fatal bugs
- **Time-inverted indicators (series-ordering bug)**: MQL5 `CopyBuffer/CopyOpen/CopyClose/CopyHigh/CopyLow` fill arrays OLDEST-FIRST unless the target array is set as a timeseries — and `bot.mq5` **never calls `ArraySetAsSeries`** (verified: 0 occurrences). Consequences:
  - `CheckMACD` (:117-144): `crossBelow = macdMain[0] < macdSignal[0] && macdMain[1] >= macdSignal[1]` (:140) and `bearishMomentum = macdMain[0] < macdMain[1]` (:141) — with `[0]`=oldest, `[1]`=newer, this detects a **bullish** cross with rising momentum, then uses it to justify a SELL. The confirmation is inverted.
  - `DetectBearishZone` (:148-186): "current candle is bearish" tests `close[0] < open[0]` of the **oldest** of 10 candles (:175); the bullish-push loop `for(i=5; i<9; …)` (:165) samples a different window than the comments claim. Every time-indexed check is scrambled.
- **Sell-limit self-contradiction**: entry requires the current bar to straddle resistance (`fvgStart=low[2] < resistance && fvgEnd=high[0] > resistance`, :272-279), then `SetSellLimit` places a SELL LIMIT **at** resistance (:285-315). A sell limit must sit ABOVE the current bid — when price is currently at/above resistance the order is rejected (invalid price). It only fills in the narrow case of an intrabar spike above resistance that already retreated, and pending orders are **never canceled** (no cleanup anywhere), so they litter until filled.
- **"FVG" is decorative**: no 3-candle gap test exists; it's the straddle test above with an ICT name. README's "sophisticated gap detection with volume and momentum confirmation" (README.md:10, :31) — there is no volume code in the repo. Also overclaimed: "Trailing Stops … configurable" (:42) — no trailing code exists; "Multi-Timeframe Analysis" (:11) — hardcoded `PERIOD_M1`.
- `.github/workflows/python-publish.yml` is leftover PyPI-publish boilerplate (repo isn't a package) — harmless, ignore.

**Anti-lessons for gold-ripper**: (a) when porting indicator logic, always verify candle ordering (the MQL5 default bit us here — Python `iloc[-2]`/shift(1) discipline already covers this); (b) never bump size up to min-lot — skip instead; (c) a kill switch that isn't fed by real fills doesn't exist; (d) ICT-named checks deserve unit tests that prove the geometry (real FVG = 3-candle gap, not a straddle).

---

## Cross-repo summary
- Only **bitget-trading-bot** touches Bitget, and its raw-v2 layer (signing, demo header, plan-order TP/SL/TPP, pending-plan reconcile, 40725 fix, true-fill re-anchor) is directly portable into gold-ripper's broker layer as ccxt fallbacks/extends.
- **aster-bot** and **Expert-Advisor-trading-bot** are junk as trading systems (dead risk gate + fake backtest; inverted arrays + sell-limit paradox + FX spread filter), but each contributes one reusable shape: the 5%-equity sizing clamp (mq5) and a cautionary tale about risk gates that never receive data (aster).
- No files in these repos were modified (audit-only; nothing "must be fixed" — they are third-party sources to mine, not maintain).
