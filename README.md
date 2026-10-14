# GOLD RIPPER 🪓🥇

**A 24/7 XAU/gold hunting bot for Bitget. Linux-first, Windows-ready. All local. Zero-trade impossible by design.**

Built from the post-mortem of gold-reaper: no impossible consensus gates, no
2-hour-per-day hunt windows, no dead ML votes — just four independent
strategy modules, mechanically enforced risk rails, venue-side stop-losses
on every entry, and walk-forward calibration on **real 26-year gold history**
fetched and trained **on your own machine**.

**v2.1.0 "merged edition"** — hardened with the best patterns mined from a
9-repo audit (freqtrade, passivbot, bitget-trading-bot, astra-quant-agent,
and five duds we learned from by NOT copying them):

| Merge | Source repo | What we took |
|---|---|---|
| **Protection framework** | freqtrade `plugins/protections` | cooldown after every close, stoploss guard, dollar-drawdown guard — file-backed locks, side-aware, never shortened. The SAME rules run in the backtest, so calibration is honest about the pauses. |
| **Backtest pessimism + anti-lookahead** | freqtrade `optimize/backtesting.py` | signals act only on closed bars, SL counted before TP on the same bar, entry-candle trailing worst-case, 500-bar warmup (never trade inside indicator warmup), plus `scripts/test_lookahead.py` proving the HTF resample is leak-free. |
| **Error ladder** | freqtrade `exchange/common.py` | fatal ccxt errors (permission / invalid / insufficient) fail FAST; transient ones retry with backoff. |
| **On-exchange SL ratchet** | freqtrade `freqtradebot.py` | stops only move if they IMPROVE, throttled to 1 venue call/minute. |
| **SpreadFilter** | freqtrade `plugins/pairlist` | entries refused when the live book is wide (news/rollover tell). |
| **Full-position TP/SL plan armor** | bitget-trading-bot `place-tpsl-order` | pos_profit/pos_loss plan orders re-attached if missing — a crashed bot can never leave a naked position. |
| **True-fill re-anchoring** | bitget-trading-bot | after a fill we poll `openPriceAvg` and anchor the ladder on the REAL average entry. |
| **Link circuit breaker** | bitget-trading-bot | 3 feed failures = ONE "LINK DOWN" alert (venue stops stay live), recovery = ONE alert. Never alert spam. |
| **Price-cross guard** | astra-quant `order_submit.py` | a limit crossing spot by >0.5% is a stale-signal hallucination — refused, taker path taken instead. |
| **R:R geometry + ladder assert** | astra-quant `order_risk.py` | stop/TP1 sides validated on every signal; `train.py` hard-DQs any candidate with TP1 < 1R (the win-small-lose-big trap that cost them 23.9 avg win vs 48.0 avg loss). |
| **Black-swan sentinel** | astra-quant `circuit_breaker.py` | a ≥3×ATR (or ≥$30) single bar suspends NEW entries until the tape calms. Stops stay live. |
| **Fail-closed indicators + ATR-shock filter** | astra-quant `signals.py` | a NaN indicator means HOLD, never trade; exploding ATR suppresses trend/breakout entries. |
| **Fail-closed state semantics** | astra-quant `cooldowns.py` | a corrupt protections store reads as BLOCKED, never as "all clear". |
| **2% sizing hysteresis** | passivbot `utils.rs` | sizing balance only moves when equity really moved ≥2% — no order re-derivation on funding jitter. |
| **Warmup discipline** | passivbot `warmup_utils.py` | backtest never trades inside the indicator warmup window. |
| **clientOid tagging** | passivbot | every order carries a tagged idempotency key for fill attribution. |
| **FastTape ms-class indicators** | (our own, audit-inspired) | O(1) incremental EMA/RSI/ATR/ADX/BB/Donchian engine — measured **4.5 µs/bar**, 3 µs snapshot (see `scripts/bench_fasttape.py`); latency rides the heartbeat `fast` field. |

Skipped on purpose: aster-bot, Expert-Advisor-trading-bot, Astra (a Roblox
item trader), Astra-algorithmic-trader (crashing coursework with an
inverted RSI rule) and astrbot (an LLM chatbot framework, not a trader) —
audit notes live in `docs/audits/`.

---

## The Rails (your mission, enforced mechanically)

The ripper never negotiates with itself. `engine/risk.py` enforces:

| Rail | Value | Behavior on hit |
|---|---|---|
| 4h block target | **+$20** | block marked DONE — no entries until next block |
| 4h block loss cap | **−$3** | block marked DEAD — no entries until next block |
| attempts per block | **3** | budget spent → block over |
| risk per trade | **$1** | 3 full stops = exactly −$3, by construction |
| day target | **+$120** | done for the day |
| day loss cap | **−$5** | done for the day |
| streak cooldown | 2 losses | 45 min pause |
| news blackout | ±10 min | no new entries around NFP/CPI/FOMC/… |

Position sizing is derived so the geometry **cannot** breach the caps:
`risk $1 / SL-distance = units`. Venue-side stop-loss is attached to every
live order (survives crashes), plus a software ladder (TP1 → breakeven →
ATR trail → time-stop) mirrors the backtest exactly.

## Daily Assurance — the zero-trade-day killer (v2.2.0)

The mission says the ripper must generate AND execute an attempt **every
day**. `engine/assurance.py` enforces it mechanically:

- From **20:00 UTC**, if the day has produced **zero fills**, entry gates
  relax on a 3-step ladder (L1 → L3, escalating every 45 min):
  ADX chop floors ×0.5 → 0, dead-tape ATR floor ×0.75 → ×0.5, mean-revert
  bands widen, pullback zones widen — until a valid signal fires.
- **Verified on real gold history**: last 2 years, 331 trading days,
  7 days had zero normal signals — **all 7 rescued by the ladder, 0
  missed** (`scripts/test_assurance.py`, 39 checks).
- Every assured signal is tagged `ASSURE-L{step}` in the journal so you
  can audit exactly which escalation level traded.
- The SAME ladder runs inside `backtest.py`, so calibration is honest
  about assured entries.

**What assurance NEVER relaxes** (the rails stay rails): loss caps,
attempt budgets, streak cooldowns, black-swan sentinel, news blackout,
spread guard, the cost-floor stop geometry (fees+slippage can never eat
the stop), and the D1 trend-bias filter.

**The two honest exceptions** (no trading system on earth can override
these): (1) if the day already hit its **−$5 loss cap**, the day is over
— that is the protection you demanded; (2) if the venue/feed is down,
nothing can place orders. On every other day, an attempt happens.

## Strategy — RIPPER-4X (four independent hunters)

Each module fires **independently** (highest score wins the bar) — there is
no ensemble consensus to deadlock:

| Module | Idea (source) | Regime |
|---|---|---|
| **M1 TREND-PULLBACK** | H1 EMA50/200 bias → pullback to EMA20 → trigger candle + ADX (the highest-WR retail gold pattern) | trending |
| **M2 MEAN-REVERT** | Bollinger(20,2) + RSI extreme fade, **ADX-filtered** (the filter is what makes reversion pay) | ranging |
| **M3 LONDON SWEEP** | Asian-range wick sweep + close back inside → reversal (≈78% of Asian false breakouts correct within 90 min of London open) | London open |
| **M4 NY BREAKOUT** | continuation through the London-morning range with ATR displacement, NY hours | NY momentum |

Exit ladder (NFI/freqtrade-inspired): bank 50% at TP1 (+1.5–2.5R), stop to
breakeven, trail the runner by ATR chandelier, time-stop stale trades.

## Training — real data, walk-forward, on your machine

```bash
python data/fetch_history.py   # 26y daily + 2.4y hourly + 70d 15m (yfinance, free)
python train.py                # 180 variants: 70% in-sample -> 30% out-of-sample
```

`train.py` writes `data/calibration.json` — the winning parameter set, its
**out-of-sample** metrics, and full provenance. `ripper.py` loads it at
boot. Re-train anytime; the trainer refuses variants with <60 OOS trades
or PF < 1.05 (no luck-based winners).

## Install — Linux (preferred)

```bash
cd gold-ripper
chmod +x install_linux.sh && ./install_linux.sh
```

- creates `.venv`, installs deps, fetches history, trains if needed
- systemd **user** services: `gold-ripper.service` (Restart=always) +
  watchdog, enabled at boot with linger → **never sleeps**
- logs: `journalctl --user -u gold-ripper -f`
- rails status: `python ripper.py --status`

## Install — Windows (optional)

```powershell
cd gold-ripper
powershell -ExecutionPolicy Bypass -File install_windows.ps1
```
Task Scheduler tasks `GoldRipper` + `GoldRipperWatchdog` (logon + boot,
auto-restart every 60s).

## Going LIVE on Bitget

1. Bitget → API keys → enable **Futures-Trade**, whitelist your IP
2. `cp .env.example .env`, then:
   - `RIPPER_MODE=LIVE`
   - `BITGET_KEY=... BITGET_SECRET=... BITGET_PASSPHRASE=...`
3. `python ripper.py` (or restart the service)
4. Symbol auto-probes **XAU/USDT:USDT** (native gold perp) → XAUT → PAXG
5. Venue clock is auto-synced (`adjustForTimeDifference`); if a server
   clock is badly drifted, raise `BITGET_RECV_WINDOW` in `.env`

Paper mode is identical logic with simulated fills (spread + slippage +
taker fees on real market data) — the same code path, like the best
open-source frameworks do it.

## Telegram alerts (optional, 60 seconds)

1. `@BotFather` → `/newbot` → copy the token
2. Message the bot once, get your id from `@userinfobot`
3. Put `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env`, then:

```bash
python engine/notify.py --test   # fires a real test alert
```

Entries (maker + taker), TP1 banks, closes, and rail transitions
(block/day DONE or DEAD) are pushed fire-and-forget — a notification
failure can never interrupt the trading loop. The dashboard's
**Telegram wire** card can send the test alert and write both keys
into `.env` for you.

## Paper stress model (fill realism)

Paper mode is deliberately pessimistic about fills so the paper PnL
never flatters the strategy:

- every taker fill crosses a **half-spread** (default 1.0 bps full spread)
- ~5% of orders hit a **spread spike** (3-6x wider — news bursts, thin book)
- every fill suffers up to **0.5 bps latency slippage** against you
- post-only maker entries do NOT cross the spread (they earn it) but the
  latency slip still applies

Tune with `PAPER_SPREAD_BPS / PAPER_SPIKE_PROB / PAPER_SLIP_BPS`, or
`PAPER_STRESS=false` to disable (not recommended — it only makes the
paper numbers prettier, never the live ones). Every stressed fill is
logged to `data/ripper_events.jsonl` with its `half_spread_bps`,
`spike` and `slip_bps` breakdown. A rolling window of the last 50
fills is published on the heartbeat (`stress:`) — the dashboard's live
status strip shows **spread paid / spike rate / slip** in real time,
plus a **per-fill sparkline** (amber = normal fill, red = spike fill)
so you can literally see the tape's microstructure. The trade ledger
surfaces the stress paid on every exit (`slip_bps` column, ⚡ icon on
spike fills) and filters the journal by exit-reason chips
(TP1 / TRAIL / SL / TIME / MANUAL).

## Drift self-check (the bot audits itself)

Every `RIPPER_SELF_CHECK_INTERVAL_S` (default 900s) the ripper re-tests
its own closed trades against the walk-forward calibration —
win-rate binomial z, expectancy z, profit-factor ratio
(`engine/divergence.py`, the same statistics the dashboard's drift
monitor uses). The verdict rides the heartbeat (`drift:`) and lands on
the dashboard as a **bot self-check** badge next to the monitor's own
independent verdict — two auditors, one dataset.

A fresh **DRIFTED** verdict pushes a Telegram alert (cooldown
`RIPPER_DRIFT_COOLDOWN_MIN`, default 360) and is logged to
`ripper_events.jsonl` as `drift_alert`. Disable entirely with
`RIPPER_SELF_CHECK=false` (not recommended — it is the tripwire that
catches a strategy dying before the balance does).

Journal schema v2: every closed trade also records `hold_s` (holding
time), `slip_bps` and `spike` (exit-fill stress). Old 14-column
journals keep working — the new columns are appended only when the
header has them.

Every verdict is also appended to `data/drift_history[_TAG].json`
(capped at 336 entries ≈ 3.5 days of checks) — the dashboard draws
this as the **self-check timeline** strip under the drift monitor,
with a **velocity** row: the score trend over the last 48 checks and
the flip count per 24h (a verdict constantly flapping between WATCH
and ALIGNED is itself a WATCH signal — stable means calm), plus a
**durations** histogram: how long each verdict usually HOLDS before
resolving, bucketed (<1h · 1-6h · 6-24h · 1-3d · >3d) with the median.
A WATCH that always clears in under an hour is sampling noise; one
that lingers past a day deserves a retrain conversation.

## Venue reconciliation (LIVE: journal vs Bitget's own ledger)

In LIVE mode the venue can fill things the software ladder never sees:
venue-side SL/TP algo orders firing between polls, a maker limit
filling during a feed stall, a position closed manually from the
Bitget app. Every `RIPPER_RECONCILE_MIN` (default 30) the ripper diffs
its journal against **Bitget's own fill history** (`fetch_my_trades`,
`engine/reconcile.py`) — a fill is "accounted for" when the journal has
a same-side open within ±6h of the venue timestamp.

- **OK** — every venue entry fill has a journal row (venue-side algo
  exits are counted separately: they close positions by design, so they
  never journal, but they ARE reported so the money is visible — each
  report carries an **estimated exit PnL priced against the matched
  journal entry, fill fees included**; Bitget's wallet history stays
  the ground truth, the estimate just turns "3 algo exits happened"
  into "algo exits banked about +$14").
- **GAP** — the venue filled an entry the journal never recorded. This
  is the silent-divergence tripwire: Telegram alert (once per GAP
  streak), `reconcile_gap` event logged, and the dashboard flips to a
  pulsing red **reconcile GAP** chip.
- **ERROR** — venue unreachable this cycle; last-good report is kept.
- Paper mode reports nothing (no venue truth to diff against).

Don't wait for the timer: `MISSION CONTROL → RECONCILE` (the
`reconcile_now` command) forces a full cycle right now — the fresh
report rides the heartbeat within one loop. In paper mode the bot acks
that reconcile is LIVE-only.

The report rides the heartbeat (`reconcile:`) and the matching window
cursor is persisted in `data/reconcile[_TAG].json` — a restart never
re-scans the same fills. Like everything else that touches the venue,
the reconcile call runs capped in a daemon thread: a hung Bitget API
can never stall the trading loop.

## Mission control (pause / resume / kill switch / retrain)

The dashboard can drive the bot through a **file-based command
channel** — no inbound port, works identically behind systemd, on a
VPS or over sshfs:

| action           | effect                                                        |
|------------------|---------------------------------------------------------------|
| `pause`          | no NEW entries (open positions still managed, rails live). **Sticky** — writes `data/pause.flag` so the pause survives crashes and restarts |
| `resume`         | clear the pause (removes `pause.flag`), hunting resumes        |
| `close_all`      | market-close every position at the tape + cancel the working limit (the kill switch) |
| `cancel_pending` | cancel the armed maker limit only                             |
| `retrain`        | re-run fetch_history + train.py **inside the bot's own interpreter/venv**; progress rides the heartbeat `train` field and the new calibration hot-reloads without a restart |
| `feed_check`     | force a fresh market-tape pull (Bitget venue → Yahoo → CSV) and ack **which layer answered, how many bars, how fast** — the "is my data alive?" button |
| `reconcile_now`  | force a venue reconciliation cycle **right now** instead of waiting the 30-min timer (LIVE only; paper acks a skip) — a fill the ladder missed can never wait in silence |

Flow: the dashboard writes `data/commands[_TAG].json` → the bot polls
it every cycle, executes, acks into `data/commands_status[_TAG].json`
and deletes the command file (a command can never run twice). A
command queued while the bot is offline expires unexecuted after 10
minutes. Every execution (and `flatten_error`, should a venue refuse
one leg) is logged to `ripper_events.jsonl`, and `pause` / `resume` /
`close_all` / `retrain` push Telegram confirmations with the rails
snapshot.

**A failed command is an alarm, not a log line.** When an ack comes
back `ok: false` — worst case the `close_all` kill switch being
rejected by the venue — the dashboard plays two descending beeps (the
audio inverse of the drift alarm, so "your emergency lever failed" is
distinguishable from "edge drifting" with eyes closed), raises a
destructive toast, and fires a desktop notification if permission has
been granted (the drift-alarm arm button grants it).

Why sticky pause matters: a paused bot that crashes overnight must
never wake up hunting on its own. With `pause.flag` on disk the pause
outlives any restart — `resume` (or `rm data/pause.flag`) is the only
way back to hunting. The dashboard shows a 🔒 `sticky` chip whenever
the flag is active.

Why retrain-via-bot matters: the dashboard's own retrain button runs
on the dashboard host — fine when everything shares a machine, wrong
when the bot lives on a remote VPS. `MISSION CONTROL → RETRAIN` sends
the command down the same file channel and the bot trains itself with
the exact interpreter/venv/data it trades with (extendable tape map
via `RIPPER_YF_MAP`). While it runs, the heartbeat carries the live
step, and the dashboard shows a spinning `bot retrain: …` chip.

CLI equivalents for the terminal-minded:

```bash
python - <<'PY'
from engine import commands
from engine.config import CONFIG
commands.write(CONFIG, "pause")   # resume / close_all / cancel_pending / retrain
PY
```

## Hardened market feed (no stall, ever — now with a venue tape)

The paper feed prices off a **tape chain** selected with `RIPPER_FEED`:

1. **Bitget venue OHLCV** (default first choice) — public market data,
   **no API keys needed**. The paper bot prices the exact XAU index
   perp that LIVE mode trades: no Yahoo futures-basis drift, no 429
   death spirals. `engine/venue_feed.py` resolves the gold perp at
   connect (XAU/USDT:USDT → XAUT → PAXG, override `BITGET_SYMBOL`).
2. **Yahoo GC=F futures** — legacy fallback, kept for offline boxes
   that can't reach Bitget.
3. **Local CSV** — fully offline last resort.

Every layer is hardened the same way — yfinance has no internal
timeout, ccxt's `load_markets()` has none either, and urllib's timeout
doesn't cover DNS stalls (all three were proven hangable) — so each
runs in a daemon thread with:

1. a **hard cap** (`RIPPER_FEED_TIMEOUT_S`, default 20s) covering the
   whole leg (connect + fetch),
2. a **single-flight guard + small cache** (`RIPPER_FEED_CACHE_S`, default
   45s) so fast pollers never hammer a venue into rate-limiting,
3. a fallback chain: **last good download → local CSV**, so the bot keeps
   pricing off the freshest bars it owns (a late success still lands in
   the cache for the next cycle).

The dashboard surfaces tape health everywhere: a `feed:` chip in LIVE
STATUS (teal = live tape, pulsing amber = DEGRADED stale fallback),
and `MISSION CONTROL → FEED CHECK` forces a fresh pull and acks
`feed OK · bitget XAU/USDT:USDT · 400 bars · 0.1s · px 4170.38`.

## Multi-symbol (run a second ripper, e.g. silver)

Every runtime file is namespaced by `RIPPER_SYMBOL_TAG`, so two
instances share nothing (rails, journal, heartbeat, calibration):

```bash
python data/fetch_history.py --tag XAG --yf-symbol SI=F
python train.py --tag XAG                      # -> data/calibration_XAG.json
RIPPER_SYMBOL_TAG=XAG BITGET_SYMBOL=XAG/USDT:USDT python ripper.py
```

Run it as its own systemd service for a 24/7 silver hunter beside gold.
Each instance enforces the rails independently on its own tape.

The dashboard follows: the **instances** switcher in the ops deck moves
the live status, rails, drift monitor AND the mission insights (equity
curve, strategy arena, trade ledger) between instances — and the
retrain button trains the selected instance on its own tape
(`fetch_history.py --tag XAG --yf-symbol SI=F` → `train.py --tag XAG`).

## Honest engineering notes

- Costs are charged in **both** backtest and paper fills (0.06%/side taker +
  0.02%/side slippage) — the trainer optimizes *net* of everything.
- The rails are extremely aggressive ($20 made on ≤$3 risked per block is a
  6.7:1 block asymmetry). The trainer maximizes what is mathematically
  achievable **within** those rails; the rails decide when to stop, the
  market decides what a block pays. All reported metrics are real
  out-of-sample numbers from your own machine — check `data/calibration.json`.
- This software trades real markets with real risk. Run paper first, verify
  the journal (`data/ripper_journal.csv`), then go live at a size you can
  afford to lose. Nothing here is financial advice.

## Map

```
ripper.py               main 24/7 loop (entry point)
engine/strategy.py      RIPPER-4X modules + exit ladder + fail-closed gates
engine/risk.py          THE RAILS (block/day engine, sizing, cooldowns)
engine/protections.py   freqtrade-merge protection locks (cooldown/SL/dd)
engine/guards.py        pre-trade guards (geometry/price-cross/spread/swan)
engine/fasttape.py      ms-class incremental indicators (FastTape)
engine/news_guard.py    economic-calendar blackout
engine/notify.py        Telegram push (fire-and-forget) + error dedupe
engine/indicators.py    ema/rsi/atr/adx/bb/donchian (pandas truth)
brokers/paper_broker.py realistic simulation (default)
brokers/bitget_broker.py  ccxt live execution + plan armor + error ladder
backtest.py             bar replay with costs + rails + protections
train.py                walk-forward grid search -> calibration.json
scripts/test_guards.py  41-test merged-code suite (guards/protections/tape)
scripts/bench_fasttape.py  microsecond latency proof
scripts/test_lookahead.py  HTF resample leak check
watchdog.py             restarts the ripper if it ever stops
install_linux.sh / install_windows.ps1
```

## Heartbeat (what the dashboard reads)

`data/heartbeat.json` is rewritten every poll cycle:
`ts · iso · version · mode · broker · symbol · started_at · uptime_s ·
price · equity (live) · rails snapshot · open · pending · news_next ·
`feed` (which tape is answering: source/bars/age/degraded) · stress
(per-fill realism window) · drift (the bot's own self-check) ·
reconcile (LIVE journal-vs-venue audit) · **prot** (active protection
locks: kind/side/minutes-left) · **fast** (FastTape snapshot: EMA/RSI/ADX/
ATR/BB/Donchian + push/snapshot latency in µs) · **link** (tape circuit
breaker state) · **swan** (black-swan verdict) · paused / sticky · train
(retrain progress) · cmd (last executed command) · last_error`. Point
any monitoring tool at it — or just read the dashboard's LIVE STATUS
panel. The same FastTape snapshot is also written standalone to
`data/tape.json` every `RIPPER_FAST_WRITE_S` seconds (default 15) so
indicators are readable AND persisted in milliseconds.

**Live wire (optional, sub-2s dashboard):** when the dashboard shares a
host with the bot, the `mini-services/ripper-wire` relay (socket.io,
port 3003) watches `data/` and pushes heartbeat + command-ack events to
the browser the moment files are written — the operator deck reacts in
milliseconds instead of waiting for the 5-10s REST polls. Delivery has
three guarantees: every push carries a monotonic **seq**; writes
landing inside one 150 ms window are **coalesced** (only the latest
version of a file flies — a burst of pushes triggers exactly one
dashboard refetch, never a storm); and the relay keeps a 250-event
ring buffer — on reconnect the browser sends `sync {lastSeq}` and
everything it missed is **replayed**, so a dropped connection no
longer silently drops the ack that mattered (the deck shows `+N` on
its wire chip when pushes were recovered this way). The relay is
a latency upgrade, never a dependency: if it is not running, the
dashboard silently falls back to polling (it will say
`5s poll (wire down)` in mission control).
