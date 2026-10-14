"""
GOLD RIPPER :: Configuration
============================
Every number the ripper lives by. Env-overridable; the trained defaults
produced by train.py live in data/calibration.json and override these at
runtime (train.py writes, ripper.py loads).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _f(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _i(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _b(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # ── identity ─────────────────────────────────────────────────────
    bot_name: str = "GOLD-RIPPER"
    version: str = "2.2.0"   # assurance edition (daily-fill guarantee)

    # ── broker ───────────────────────────────────────────────────────
    mode: str = os.getenv("RIPPER_MODE", "PAPER").upper()      # PAPER | LIVE
    exchange: str = os.getenv("BITGET_EXCHANGE", "bitget")
    bitget_key: str = os.getenv("BITGET_KEY", "")
    bitget_secret: str = os.getenv("BITGET_SECRET", "")
    bitget_passphrase: str = os.getenv("BITGET_PASSPHRASE", "")
    # Bitget lists a native XAUUSDT perp (tracks spot gold index).
    # Candidates are probed in order at connect time.
    bitget_symbol_candidates: tuple = (
        "XAU/USDT:USDT", "XAUT/USDT:USDT", "PAXG/USDT:USDT")
    bitget_symbol: str = os.getenv("BITGET_SYMBOL", "")
    bitget_leverage: int = _i("BITGET_LEVERAGE", 3)
    bitget_margin_coin: str = os.getenv("BITGET_MARGIN_COIN", "USDT")
    # request window: bump if the venue complains about clock skew
    bitget_recv_window: int = _i("BITGET_RECV_WINDOW", 20000)

    # ── multi-instance tag (optional) ────────────────────────────────
    # Run a SECOND ripper on another symbol (e.g. silver) beside the gold
    # one: RIPPER_SYMBOL_TAG=XAG BITGET_SYMBOL=XAG/USDT:USDT python ripper.py
    # Every runtime file (state, journal, heartbeat, calibration) gets the
    # tag suffix, so the two instances never touch each other's rails.
    symbol_tag: str = os.getenv("RIPPER_SYMBOL_TAG", "").strip().upper()

    # ── THE RAILS (user mission, enforced by engine/risk.py) ─────────
    block_target_usd: float = _f("BLOCK_TARGET_USD", 20.0)     # +$20 per 4h block
    block_max_loss_usd: float = _f("BLOCK_MAX_LOSS_USD", 3.0)  # -$3 kills the block
    day_target_usd: float = _f("DAY_TARGET_USD", 120.0)        # +$120 per day
    day_max_loss_usd: float = _f("DAY_MAX_LOSS_USD", 5.0)      # -$5 kills the day
    max_attempts_per_block: int = _i("MAX_ATTEMPTS_PER_BLOCK", 3)
    max_open_trades: int = _i("MAX_OPEN_TRADES", 1)
    risk_usd_per_trade: float = _f("RISK_USD_PER_TRADE", 1.0)  # $1 risk -> 3 tries = $3
    max_risk_usd_per_trade: float = _f("MAX_RISK_USD_PER_TRADE", 1.5)
    consec_loss_pause_min: int = _i("CONSEC_LOSS_PAUSE_MIN", 45)
    consec_loss_pause_after: int = _i("CONSEC_LOSS_PAUSE_AFTER", 2)
    starting_balance: float = _f("STARTING_BALANCE", 1000.0)

    # ── data ─────────────────────────────────────────────────────────
    poll_seconds: int = _i("POLL_SECONDS", 60)
    live_candle_csv: str = "data/history/gold_1h.csv"
    # market-data tape (paper mode): the Bitget venue OHLCV needs NO API
    # keys and prices the exact perp live mode trades - no Yahoo futures
    # basis, no 429 death spirals.
    #   auto  (default) venue -> yahoo -> local CSV
    #   venue | yahoo | csv   pin one tape explicitly
    feed_source: str = os.getenv("RIPPER_FEED", "auto").strip().lower()
    venue_ohlcv_limit: int = _i("RIPPER_VENUE_BARS", 400)
    # feed hardening: a hung feed can never stall the trading loop - the
    # download runs in a daemon thread with a hard cap, a small cache and
    # a last-good-download -> CSV fallback.
    feed_timeout_s: int = _i("RIPPER_FEED_TIMEOUT_S", 20)
    feed_cache_s: int = _i("RIPPER_FEED_CACHE_S", 45)

    # ── strategy: global gates ───────────────────────────────────────
    min_atr_usd: float = _f("MIN_ATR_USD", 2.0)     # skip dead tape
    # cost-aware stop floor: a stop closer than this is mathematically
    # untradeable (taker fees + slippage exceed the edge)
    risk_floor_atr: float = _f("RISK_FLOOR_ATR", 1.2)   # min stop dist in ATR
    risk_floor_pct: float = _f("RISK_FLOOR_PCT", 0.006)  # min stop dist as price frac
    # trend-bias filter: in an H1 EMA200 uptrend only LONGs fire, in a
    # downtrend only SHORTs. Gold 2024-2026: +87% - fading the tape is how
    # retail bots die. The trainer can disable it (BIAS_FILTER=false).
    bias_filter: bool = _b("BIAS_FILTER", True)
    trade_asia: bool = _b("TRADE_ASIA", True)   # 24h hunting - Asia breakouts pay
    trade_london: bool = _b("TRADE_LONDON", True)
    trade_overlap: bool = _b("TRADE_OVERLAP", True)
    trade_newyork: bool = _b("TRADE_NEWYORK", True)
    trade_late_us: bool = _b("TRADE_LATE_US", False)

    # ── module 1: trend pullback ─────────────────────────────────────
    m1_enabled: bool = True
    m1_ema_fast: int = 50
    m1_ema_slow: int = 200
    m1_ema_pull: int = 20
    m1_near_atr: float = 0.5          # pullback proximity (x ATR)
    m1_rsi_long: tuple = (38.0, 60.0)
    m1_rsi_short: tuple = (40.0, 62.0)
    m1_min_adx: float = 22.0
    m1_sl_atr: float = 4.5
    m1_tp1_r: float = 1.2             # TP1 banks 50% at +1.2R
    m1_trail_atr: float = 5.0
    m1_time_stop_bars: int = 120

    # ── module 2: bollinger + rsi mean reversion ────────────────────
    m2_enabled: bool = True
    m2_max_adx: float = 24.0
    m2_bb_n: int = 20
    m2_bb_k: float = 2.0
    m2_bb_lo: float = 0.06
    m2_bb_hi: float = 0.94
    m2_rsi_lo: float = 31.0
    m2_rsi_hi: float = 69.0
    m2_sl_atr: float = 1.6
    m2_tp1_r: float = 1.0             # quick bank (mean reversion pays fast)
    m2_trail_atr: float = 2.2
    m2_time_stop_bars: int = 14

    # ── module 3: london sweep reversal ──────────────────────────────
    m3_enabled: bool = True
    m3_asia_start: int = 0            # UTC hours defining the asian range
    m3_asia_end: int = 7
    m3_window: tuple = (7, 11)        # trade 07:00-10:59 UTC
    m3_min_range: float = 4.0         # $ range sanity
    m3_max_range: float = 60.0
    m3_sl_buffer_atr: float = 0.25
    m3_min_wick_atr: float = 0.5      # a REAL sweep wick, not noise
    m3_tp1_r: float = 1.0
    m3_trail_atr: float = 2.6
    m3_time_stop_bars: int = 12

    # ── module 4: NY continuation breakout ───────────────────────────
    m4_enabled: bool = True
    m4_window: tuple = (13, 17)       # 13:00-16:59 UTC (NY morning)
    m4_lookback: int = 6              # london-morning range length (bars)
    m4_body_atr: float = 1.1          # displacement body >= x ATR
    m4_min_adx: float = 18.0
    m4_sl_atr: float = 2.0
    m4_tp1_r: float = 1.0
    m4_trail_atr: float = 3.0
    m4_time_stop_bars: int = 36

    # ── exit ladder (shared) ─────────────────────────────────────────
    tp1_fraction: float = 0.5         # bank 50% at TP1
    breakeven_buffer_usd: float = 0.30
    trail_start_r: float = 1.2        # runner trails right after TP1

    # ── news guard ───────────────────────────────────────────────────
    news_guard: bool = _b("NEWS_GUARD", True)
    news_blackout_min: int = _i("NEWS_BLACKOUT_MIN", 10)
    news_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    # ── costs (Bitget: taker 0.06%, maker 0.02%) ─────────────────────
    fee_rate: float = _f("FEE_RATE", 0.0006)        # taker, per side
    fee_maker: float = _f("FEE_MAKER", 0.0002)      # maker, per side
    slippage_rate: float = _f("SLIPPAGE_RATE", 0.0002)  # per side
    # maker entry: post-only limit at the signal close (better fee AND
    # fill price). Unfilled orders expire after N bars.
    maker_entry: bool = _b("MAKER_ENTRY", True)
    maker_wait_bars: int = _i("MAKER_WAIT_BARS", 3)

    # ── protections (merged from freqtrade's protection framework) ────
    # cooldown after every full close, stoploss guard, dollar drawdown
    # guard. The SAME rules run in the backtest so calibration is honest
    # about the pauses live trading takes.
    prot_enabled: bool = _b("PROT_ENABLED", True)
    prot_cooldown_bars: int = _i("PROT_COOLDOWN_BARS", 2)      # H1 bars
    prot_sl_window_bars: int = _i("PROT_SL_WINDOW_BARS", 24)
    prot_sl_max: int = _i("PROT_SL_MAX", 2)
    prot_sl_pause_bars: int = _i("PROT_SL_PAUSE_BARS", 4)
    prot_dd_window_bars: int = _i("PROT_DD_WINDOW_BARS", 24)
    prot_dd_max_usd: float = _f("PROT_DD_MAX_USD", 6.0)

    # ── daily assurance (engine/assurance.py) ─────────────────────────
    # THE ZERO-TRADE-DAY KILLER: if no entry has filled today by
    # assure_hour_utc, entry gates progressively relax (L1..L3) until at
    # least one risk-sized attempt happens EVERY day. Safety rails are
    # NEVER relaxed: loss caps, attempt budgets, streak cooldowns, black
    # swan, news blackout, spread guard, cost-floor geometry, D1 bias.
    assure_daily: bool = _b("ASSURE_DAILY", True)
    assure_hour_utc: int = _i("ASSURE_HOUR_UTC", 20)   # start 20:00 UTC
    assure_steps: int = _i("ASSURE_STEPS", 3)          # L1..L3 ladder
    assure_escalate_min: int = _i("ASSURE_ESCALATE_MIN", 45)  # per step

    # ── fast lane (engine/fasttape.py, ms-class indicators) ────────
    fast_lane: bool = _b("RIPPER_FAST_LANE", True)
    fast_write_s: int = _i("RIPPER_FAST_WRITE_S", 15)  # tape.json cadence

    # ── venue plan armor (bitget-trading-bot merge, LIVE only) ─────
    # keep full-position TP/SL plan orders attached on the venue and
    # re-place any leg that goes missing (a crash must never naked us)
    plans_armor: bool = _b("BITGET_PLANS_ARMOR", True)
    plans_check_s: int = _i("BITGET_PLANS_CHECK_S", 60)

    # ── command channel (dashboard -> bot, file-based, no inbound port) ─
    # The dashboard writes commands.json (pause/resume/close_all/retrain),
    # the bot polls, executes, acks into commands_status.json and deletes it.
    commands_file: Path = ROOT / "data" / "commands.json"
    commands_status_file: Path = ROOT / "data" / "commands_status.json"
    # STICKY pause: pause.flag survives a bot restart (a paused bot that
    # crashes overnight must NOT wake up hunting at 3am). resume removes it.
    pause_file: Path = ROOT / "data" / "pause.flag"

    # ── drift self-check (the bot audits its own fills) ───────────────
    # Every selfcheck_interval_s the journal is re-tested against the
    # walk-forward calibration (see engine/divergence.py). On a DRIFTED
    # verdict a Telegram alert fires (cooldown-guarded).
    self_check: bool = _b("RIPPER_SELF_CHECK", True)
    selfcheck_interval_s: int = _i("RIPPER_SELF_CHECK_INTERVAL_S", 900)
    drift_alert_cooldown_min: int = _i("RIPPER_DRIFT_COOLDOWN_MIN", 360)

    # ── venue reconciliation (LIVE: journal vs Bitget fill history) ────
    # The venue can fill things the software ladder never sees (venue-side
    # SL/TP between polls, a fill during a feed stall, a manual close from
    # the Bitget app). Every reconcile_min the journal is diffed against
    # fetch_my_trades - a GAP pushes a Telegram alert. Paper: SKIP.
    reconcile_min: int = _i("RIPPER_RECONCILE_MIN", 30)
    reconcile_alert_cooldown_min: int = _i("RIPPER_RECONCILE_COOLDOWN_MIN", 360)
    reconcile_state_file: Path = ROOT / "data" / "reconcile.json"

    # ── paper stress model (paper-mode fill realism) ──────────────────
    # Paper fills cross a half-spread, occasionally hit spread spikes
    # (news bursts / thin book) and suffer latency slippage - so the paper
    # PnL never flatters the strategy. Disable with PAPER_STRESS=false.
    paper_stress: bool = _b("PAPER_STRESS", True)
    paper_spread_bps: float = _f("PAPER_SPREAD_BPS", 1.0)   # full spread, bps
    paper_spike_prob: float = _f("PAPER_SPIKE_PROB", 0.05)  # per order
    paper_spike_min_x: float = _f("PAPER_SPIKE_MIN_X", 3.0)  # spike widening
    paper_spike_max_x: float = _f("PAPER_SPIKE_MAX_X", 6.0)
    paper_slip_bps: float = _f("PAPER_SLIP_BPS", 0.5)        # max latency slip

    # ── runtime files ────────────────────────────────────────────────
    state_file: Path = ROOT / "data" / "ripper_state.json"
    journal_file: Path = ROOT / "data" / "ripper_journal.csv"
    heartbeat_file: Path = ROOT / "data" / "heartbeat.json"
    calibration_file: Path = ROOT / "data" / "calibration.json"
    train_status_file: Path = ROOT / "data" / "train_status.json"
    drift_history_file: Path = ROOT / "data" / "drift_history.json"
    protections_file: Path = ROOT / "data" / "protections.json"
    tape_file: Path = ROOT / "data" / "tape.json"

    def __post_init__(self) -> None:
        if self.symbol_tag:
            def tagged(p: Path) -> Path:
                return p.with_name(f"{p.stem}_{self.symbol_tag}{p.suffix}")
            self.state_file = tagged(self.state_file)
            self.journal_file = tagged(self.journal_file)
            self.heartbeat_file = tagged(self.heartbeat_file)
            self.calibration_file = tagged(self.calibration_file)
            self.train_status_file = tagged(self.train_status_file)
            self.commands_file = tagged(self.commands_file)
            self.commands_status_file = tagged(self.commands_status_file)
            self.pause_file = tagged(self.pause_file)
            self.drift_history_file = tagged(self.drift_history_file)
            self.reconcile_state_file = tagged(self.reconcile_state_file)
            self.protections_file = tagged(self.protections_file)
            self.tape_file = tagged(self.tape_file)

    def session_allowed(self, session: str) -> bool:
        return {
            "ASIA": self.trade_asia, "LONDON": self.trade_london,
            "OVERLAP": self.trade_overlap, "NEWYORK": self.trade_newyork,
            "LATE_US": self.trade_late_us,
        }.get(session, False)

    def apply_calibration(self, path: Path | None = None) -> bool:
        """Overlay trained parameters from data/calibration.json."""
        p = path or self.calibration_file
        try:
            data = json.loads(Path(p).read_text())
        except Exception:  # noqa: BLE001
            return False
        params = data.get("params", {})
        applied = 0
        for k, v in params.items():
            if hasattr(self, k):
                cur = getattr(self, k)
                if isinstance(cur, tuple) and isinstance(v, list):
                    v = tuple(v)
                try:
                    setattr(self, k, v)
                    applied += 1
                except (TypeError, ValueError):
                    pass
        return applied > 0


CONFIG = Config()
