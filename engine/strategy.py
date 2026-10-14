"""
GOLD RIPPER :: Strategy Engine
==============================
Four independent hunting modules, each with its own gates, each able to
fire on its own (no impossible-consensus design). Every bar, every enabled
module gets a look; the highest-scoring valid signal wins the bar.

  M1 TREND-PULLBACK   bias H1 EMA50/200 -> pullback to EMA20 -> trigger
  M2 MEAN-REVERT      BB(20,2) + RSI extremes, only when ADX says range
  M3 LONDON SWEEP     asian-range wick sweep + close back inside -> reversal
  M4 NY BREAKOUT      continuation through the London-morning range, NY hours

Exits are a ladder (TP1 banks tp1_fraction at tp1_r, SL -> breakeven,
runner trails by ATR chandelier, time-stop closes stale trades).

Every emitted Signal is fully self-describing (module, score, reason) so
the journal can be audited trade-by-trade.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from engine import assurance
from engine import guards
from engine.config import Config
from engine.indicators import (adx, atr, bb_pos, ema, rsi, session_of,
                               swings)


@dataclass
class Signal:
    side: str              # LONG | SHORT
    entry: float
    sl: float
    tp1: float
    module: str
    score: float
    session: str
    ts: pd.Timestamp
    reason: str
    sl_atr_mult: float = 1.2
    trail_atr_mult: float = 2.0
    time_stop_bars: int = 12
    tp1_r: float = 1.0

    @property
    def risk_dist(self) -> float:
        return abs(self.entry - self.sl)


@dataclass
class Position:
    side: str
    entry: float
    sl: float
    tp1: float
    size: float            # oz / units
    module: str
    opened_ts: pd.Timestamp
    opened_bar: int
    tp1_done: bool = False
    be_moved: bool = False
    orig_sl: float = 0.0
    peak_r: float = 0.0
    remaining: float = 1.0     # fraction of original size still open
    trail_atr_mult: float = 2.0
    time_stop_bars: int = 12
    meta: dict = field(default_factory=dict)


class StrategyEngine:
    name = "RIPPER-4X"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------ shared precompute
    def _context(self, df: pd.DataFrame) -> dict:
        c = df["close"]
        # higher-timeframe bias, resampled from H1 (works in backtest AND
        # live on the same feed - no second data source needed)
        d1c = c.resample("1D").last().dropna()
        d1_ema20 = ema(d1c, 20).shift(1).reindex(c.index, method="ffill")
        h4c = c.resample("4h").last().dropna()
        h4f = ema(h4c, 20).shift(1).reindex(c.index, method="ffill")
        h4s = ema(h4c, 50).shift(1).reindex(c.index, method="ffill")
        dc_hi = df["high"].rolling(24).max().shift(1)
        dc_lo = df["low"].rolling(24).min().shift(1)
        return {
            "ema_f": ema(c, self.cfg.m1_ema_fast),
            "ema_s": ema(c, self.cfg.m1_ema_slow),
            "ema_p": ema(c, self.cfg.m1_ema_pull),
            "rsi": rsi(c, 14),
            "adx": adx(df, 14),
            "atr": atr(df, 14),
            "bbp": bb_pos(c, self.cfg.m2_bb_n, self.cfg.m2_bb_k),
            "d1_ema20": d1_ema20,
            "h4_fast": h4f, "h4_slow": h4s,
            "dc_hi": dc_hi, "dc_lo": dc_lo,
        }

    # ------------------------------------------------------------ module 1
    def _m1_trend_pullback(self, df: pd.DataFrame, ctx: dict, i: int,
                           ts) -> Signal | None:
        """M1 DONCHIAN TREND RIDER - the validated core edge.
        Breakout continuation with daily-trend bias, wide stop, TP1 bank,
        long ATR trail (probe: PF 1.8-1.9 raw on 2.4y of H1 gold)."""
        cfg = self.cfg
        px = float(df["close"].iloc[i])
        a = float(ctx["atr"].iloc[i])
        d1b = float(ctx["d1_ema20"].iloc[i]) if ctx["d1_ema20"] is not None else None
        dc_hi = float(ctx["dc_hi"].iloc[i]) if not pd.isna(ctx["dc_hi"].iloc[i]) else None
        dc_lo = float(ctx["dc_lo"].iloc[i]) if not pd.isna(ctx["dc_lo"].iloc[i]) else None
        if a < cfg.min_atr_usd or d1b is None or dc_hi is None:
            return None
        if px > dc_hi and px > d1b:
            sl = px - cfg.m1_sl_atr * a
            return Signal("LONG", px, sl, px + cfg.m1_tp1_r * (px - sl),
                          "M1_TREND", 0.78, session_of(ts), ts,
                          f"donchian24 breakout {dc_hi:.2f} d1-up",
                          trail_atr_mult=cfg.m1_trail_atr,
                          time_stop_bars=cfg.m1_time_stop_bars,
                          tp1_r=cfg.m1_tp1_r)
        if px < dc_lo and px < d1b:
            sl = px + cfg.m1_sl_atr * a
            return Signal("SHORT", px, sl, px - cfg.m1_tp1_r * (sl - px),
                          "M1_TREND", 0.78, session_of(ts), ts,
                          f"donchian24 breakdown {dc_lo:.2f} d1-dn",
                          trail_atr_mult=cfg.m1_trail_atr,
                          time_stop_bars=cfg.m1_time_stop_bars,
                          tp1_r=cfg.m1_tp1_r)
        return None

    # ------------------------------------------------------------ module 2
    def _m2_meanrev(self, df: pd.DataFrame, ctx: dict, i: int, ts) -> Signal | None:
        cfg = self.cfg
        px = float(df["close"].iloc[i])
        a = float(ctx["atr"].iloc[i])
        dx = float(ctx["adx"].iloc[i])
        r = float(ctx["rsi"].iloc[i])
        bp = float(ctx["bbp"].iloc[i])
        o = float(df["open"].iloc[i])

        if a < cfg.min_atr_usd or dx > cfg.m2_max_adx:
            return None
        # reversal candle requirement: close back off the extreme
        bull_reject = px > o
        if bp <= cfg.m2_bb_lo and r <= cfg.m2_rsi_lo and bull_reject:
            sl = float(df["low"].iloc[i]) - cfg.m2_sl_atr * a * 0.5
            return Signal("LONG", px, sl, px + cfg.m2_tp1_r * (px - sl),
                          "M2_MEANREV", 0.62, session_of(ts), ts,
                          f"meanrev bbp {bp:.2f} rsi {r:.0f} adx {dx:.0f}",
                          cfg.m2_sl_atr, cfg.m2_trail_atr, cfg.m2_time_stop_bars,
                          cfg.m2_tp1_r)
        if bp >= cfg.m2_bb_hi and r >= cfg.m2_rsi_hi and not bull_reject:
            sl = float(df["high"].iloc[i]) + cfg.m2_sl_atr * a * 0.5
            return Signal("SHORT", px, sl, px - cfg.m2_tp1_r * (sl - px),
                          "M2_MEANREV", 0.62, session_of(ts), ts,
                          f"meanrev bbp {bp:.2f} rsi {r:.0f} adx {dx:.0f}",
                          cfg.m2_sl_atr, cfg.m2_trail_atr, cfg.m2_time_stop_bars,
                          cfg.m2_tp1_r)
        return None

    # ------------------------------------------------------------ module 3
    def _m3_london_sweep(self, df: pd.DataFrame, ctx: dict, i: int, ts) -> Signal | None:
        cfg = self.cfg
        h = int(ts.hour)
        if not (cfg.m3_window[0] <= h < cfg.m3_window[1]):
            return None
        a = float(ctx["atr"].iloc[i])
        if a < cfg.min_atr_usd:
            return None
        # asian range from the same UTC day
        day_start = ts.normalize() + pd.Timedelta(hours=cfg.m3_asia_start)
        day_end = day_start + pd.Timedelta(hours=cfg.m3_asia_end)
        mask = (df.index >= day_start) & (df.index < day_end)
        asia = df.loc[mask]
        if len(asia) < 3:
            return None
        a_hi = float(asia["high"].max())
        a_lo = float(asia["low"].min())
        rng = a_hi - a_lo
        if not (cfg.m3_min_range <= rng <= cfg.m3_max_range):
            return None

        hi_i = float(df["high"].iloc[i])
        lo_i = float(df["low"].iloc[i])
        px = float(df["close"].iloc[i])
        upper_wick = hi_i - max(px, float(df["open"].iloc[i]))
        lower_wick = min(px, float(df["open"].iloc[i])) - lo_i
        # sweep up: REAL wick above asian high, close back inside -> SHORT
        if hi_i > a_hi and px < a_hi and \
                upper_wick >= cfg.m3_min_wick_atr * a:
            sl = hi_i + cfg.m3_sl_buffer_atr * a
            risk = sl - px
            return Signal("SHORT", px, sl, px - cfg.m3_tp1_r * risk,
                          "M3_SWEEP", 0.68, session_of(ts), ts,
                          f"london sweep-up of asian high {a_hi:.2f}",
                          trail_atr_mult=cfg.m3_trail_atr,
                          time_stop_bars=cfg.m3_time_stop_bars,
                          tp1_r=cfg.m3_tp1_r)
        # sweep down: REAL wick below asian low, close back inside -> LONG
        if lo_i < a_lo and px > a_lo and \
                lower_wick >= cfg.m3_min_wick_atr * a:
            sl = lo_i - cfg.m3_sl_buffer_atr * a
            risk = px - sl
            return Signal("LONG", px, sl, px + cfg.m3_tp1_r * risk,
                          "M3_SWEEP", 0.68, session_of(ts), ts,
                          f"london sweep-dn of asian low {a_lo:.2f}",
                          trail_atr_mult=cfg.m3_trail_atr,
                          time_stop_bars=cfg.m3_time_stop_bars,
                          tp1_r=cfg.m3_tp1_r)
        return None

    # ------------------------------------------------------------ module 4
    def _m4_ny_breakout(self, df: pd.DataFrame, ctx: dict, i: int, ts) -> Signal | None:
        """NY continuation through the London-morning range, H4-biased
        (probe: PF ~1.3 raw)."""
        cfg = self.cfg
        h = int(ts.hour)
        if not (cfg.m4_window[0] <= h < cfg.m4_window[1]):
            return None
        if i < cfg.m4_lookback + 1:
            return None
        a = float(ctx["atr"].iloc[i])
        if a < cfg.min_atr_usd:
            return None
        h4f = ctx["h4_fast"].iloc[i]
        h4s = ctx["h4_slow"].iloc[i]
        if h4f is None or pd.isna(h4f) or pd.isna(h4s):
            return None
        lo_i = i - cfg.m4_lookback
        rng_hi = float(df["high"].iloc[lo_i:i].max())
        rng_lo = float(df["low"].iloc[lo_i:i].min())
        px = float(df["close"].iloc[i])
        o = float(df["open"].iloc[i])
        body = abs(px - o)
        if body < cfg.m4_body_atr * a:
            return None
        h4_up = float(h4f) > float(h4s)
        if px > rng_hi and px > o and h4_up:
            sl = px - cfg.m4_sl_atr * a
            return Signal("LONG", px, sl, px + cfg.m4_tp1_r * (px - sl),
                          "M4_NYBREAK", 0.70, session_of(ts), ts,
                          f"NY continuation {rng_hi:.2f} h4-up body {body / a:.1f}xATR",
                          trail_atr_mult=cfg.m4_trail_atr,
                          time_stop_bars=cfg.m4_time_stop_bars,
                          tp1_r=cfg.m4_tp1_r)
        if px < rng_lo and px < o and not h4_up:
            sl = px + cfg.m4_sl_atr * a
            return Signal("SHORT", px, sl, px - cfg.m4_tp1_r * (sl - px),
                          "M4_NYBREAK", 0.70, session_of(ts), ts,
                          f"NY continuation {rng_lo:.2f} h4-dn body {body / a:.1f}xATR",
                          trail_atr_mult=cfg.m4_trail_atr,
                          time_stop_bars=cfg.m4_time_stop_bars,
                          tp1_r=cfg.m4_tp1_r)
        return None

    # ------------------------------------------------------------ helpers
    def _apply_floor(self, sig: Signal, atr_v: float) -> Signal:
        """Cost-aware stop floor: widen the SL to at least
        max(risk_floor_atr x ATR, risk_floor_pct x price). A stop closer
        than this loses to taker fees before the edge can pay."""
        cfg = self.cfg
        floor = max(cfg.risk_floor_atr * atr_v,
                    cfg.risk_floor_pct * sig.entry)
        if sig.risk_dist >= floor:
            return sig
        if sig.side == "LONG":
            sig.sl = sig.entry - floor
        else:
            sig.sl = sig.entry + floor
        sig.tp1 = sig.entry + (floor * sig.tp1_r if sig.side == "LONG"
                               else -floor * sig.tp1_r)
        return sig

    # ------------------------------------------------------------ evaluate one bar
    def evaluate(self, df: pd.DataFrame, ctx: dict | None, i: int,
                 ts, assure: int = 0) -> Signal | None:
        """Run all enabled modules on bar i. Highest score wins.

        assure: Daily-Assurance escalation level 0..3 (engine/assurance.py).
        >0 means the day had no fill yet and the clock passed the assure
        hour - entry gates relax by the ladder while every safety rail
        stays hard. Relaxed signals are tagged ASSURE-L{step}.

        Merged guards (9-repo audit):
          - fail-closed indicators: a NaN indicator HOLDs, never trades
          - module chop guard: ADX floor when the param asks for one
          - ATR shock filter: exploding ATR suppresses trend/breakout
        """
        cfg = self.cfg
        if assure and assure > 0:
            # relax a COPY of the config for this evaluation - the live
            # cfg object (and calibration defaults) are never mutated
            cfg = assurance.relaxed_config(cfg, assure)
        session = session_of(ts)
        if not cfg.session_allowed(session):
            return None
        if ctx is None:
            ctx = self._context(df)
        atr_v = float(ctx["atr"].iloc[i])
        px_now = float(df["close"].iloc[i])
        d1_bias = float(ctx["d1_ema20"].iloc[i]) if ctx["d1_ema20"] is not None and not pd.isna(ctx["d1_ema20"].iloc[i]) else None

        # fail-closed indicator gate (astra-quant signals.py merge): gate
        # ONLY what an enabled module actually reads; anything NaN/inf
        # means HOLD - missing data never becomes a trade.
        needed: dict[str, float] = {"atr": atr_v}
        want_adx = cfg.m2_enabled or \
            (cfg.m1_enabled and cfg.m1_min_adx > 0) or \
            (cfg.m4_enabled and cfg.m4_min_adx > 0)
        for key, on in (("rsi", cfg.m2_enabled), ("bbp", cfg.m2_enabled),
                        ("adx", want_adx),
                        ("h4_fast", cfg.m4_enabled),
                        ("h4_slow", cfg.m4_enabled)):
            if on:
                try:
                    needed[key] = float(ctx[key].iloc[i])
                except (KeyError, IndexError, TypeError, ValueError):
                    needed[key] = float("nan")
        ok, _why = guards.indicators_ready(needed)
        if not ok:
            return None
        # ATR shock filter (astra-quant signals.py merge): ATR exploding
        # vs its own median -> trend/breakout entries stand down
        shocked, _swhy = guards.shock(ctx["atr"], i)
        dx = float(ctx["adx"].iloc[i])

        candidates = self._harvest(df, ctx, i, ts, cfg, assure, px_now,
                                   d1_bias, shocked, dx)
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.score, reverse=True)
        best = candidates[0]
        if assure and assure > 0:
            best.reason = f"{best.reason} | ASSURE-L{int(assure)}"
        # final structural geometry assert (astra-quant merge): the ripper
        # never emits a signal with non-finite or wrong-sided levels
        # (min_tp1_r=0 here - the strict ladder rule lives in train.py)
        gok, gwhy = guards.validate_geometry(best.side, float(best.entry),
                                             float(best.sl), float(best.tp1),
                                             float(best.tp1_r), min_tp1_r=0.0)
        if not gok:
            return None
        if atr_v > 0:
            best = self._apply_floor(best, atr_v)
        return best

    # ------------------------------------------------------------ module harvest
    def _harvest(self, df: pd.DataFrame, ctx: dict, i: int, ts,
                 cfg: Config, assure: int, px_now: float,
                 d1_bias: float | None, shocked: bool, dx: float
                 ) -> list[Signal]:
        """Run every enabled module + its post-gates, collect candidates.

        Daily assurance: when assure > 0 the RELAXED config copy is
        swapped onto self.cfg for the module calls (modules read
        self.cfg directly) and restored in a finally - the live config
        object can never stay mutated, even if a module raises.
        """
        swapped = False
        if assure and assure > 0:
            self._orig_cfg = self.cfg
            self.cfg = cfg
            swapped = True
        candidates: list[Signal] = []
        try:
            for fn, enabled in (
                    (self._m1_trend_pullback, cfg.m1_enabled),
                    (self._m2_meanrev, cfg.m2_enabled),
                    (self._m3_london_sweep, cfg.m3_enabled),
                    (self._m4_ny_breakout, cfg.m4_enabled)):
                if not enabled:
                    continue
                try:
                    sig = fn(df, ctx, i, ts)
                    if sig is not None:
                        # trend-bias gate: never fight the H1 EMA200 tape
                        if cfg.bias_filter and d1_bias is not None:
                            if sig.side == "LONG" and px_now < d1_bias:
                                continue
                            if sig.side == "SHORT" and px_now > d1_bias:
                                continue
                        # module chop guard: ADX floor only when the
                        # param actually asks for one (calibration is
                        # free to set 0 - assurance may set it to 0 too)
                        if sig.module == "M1_TREND" and cfg.m1_min_adx > 0 \
                                and dx < cfg.m1_min_adx:
                            continue
                        if sig.module == "M4_NYBREAK" and \
                                cfg.m4_min_adx > 0 and dx < cfg.m4_min_adx:
                            continue
                        # shock guard: no trend/breakout chasing on quake
                        if shocked and sig.module in ("M1_TREND",
                                                      "M4_NYBREAK"):
                            continue
                        candidates.append(sig)
                except (IndexError, KeyError, ValueError):
                    continue
        finally:
            if swapped:
                self.cfg = self._orig_cfg
        return candidates

    # ------------------------------------------------------------ exit ladder
    def manage(self, pos: Position, df: pd.DataFrame, i: int, atr_v: float
               ) -> tuple[str, float | None, float]:
        """
        Returns (action, price, realized_fraction).
        action: HOLD | TP1 | BE | TRAIL | EXIT_SL | EXIT_TP | TIME
        Conservative rule: if SL and profit target hit in the same bar,
        the SL is counted FIRST (never flatter ourselves).
        """
        cfg = self.cfg
        bar_h = float(df["high"].iloc[i])
        bar_l = float(df["low"].iloc[i])
        r_dist = abs(pos.entry - pos.orig_sl) if pos.orig_sl else \
            abs(pos.entry - pos.sl)

        # SL first (conservative)
        if pos.side == "LONG" and bar_l <= pos.sl:
            return "EXIT_SL", pos.sl, 1.0
        if pos.side == "SHORT" and bar_h >= pos.sl:
            return "EXIT_SL", pos.sl, 1.0

        move = (bar_h - pos.entry) if pos.side == "LONG" else (pos.entry - bar_l)
        r_now = move / r_dist if r_dist > 0 else 0.0
        pos.peak_r = max(pos.peak_r, r_now)

        # TP1: partial bank + move stop to breakeven
        if not pos.tp1_done:
            if (pos.side == "LONG" and bar_h >= pos.tp1) or \
                    (pos.side == "SHORT" and bar_l <= pos.tp1):
                return "TP1", pos.tp1, cfg.tp1_fraction

        # time stop
        if (i - pos.opened_bar) >= pos.time_stop_bars:
            px = float(df["close"].iloc[i])
            return "TIME", px, 1.0

        # breakeven after TP1
        if pos.tp1_done and not pos.be_moved:
            be = pos.entry + (cfg.breakeven_buffer_usd if pos.side == "LONG"
                              else -cfg.breakeven_buffer_usd)
            return "BE", be, 0.0

        # trail the runner (immediately when trail_start_r <= 0 = no-TP1 mode)
        if (pos.tp1_done or cfg.trail_start_r <= 0) and \
                pos.peak_r >= max(cfg.trail_start_r, 0.0):
            if pos.side == "LONG":
                trail = bar_h - pos.trail_atr_mult * atr_v
                if trail > pos.sl:
                    return "TRAIL", trail, 0.0
            else:
                trail = bar_l + pos.trail_atr_mult * atr_v
                if trail < pos.sl:
                    return "TRAIL", trail, 0.0
        return "HOLD", None, 0.0
