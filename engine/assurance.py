"""
GOLD RIPPER :: Daily Assurance
==============================
The zero-trade-day killer. The user's mission says the ripper must
generate AND execute an attempt EVERY day - so if the day is nearly over
and nothing has filled yet, the engine progressively relaxes the
*selective* entry gates (chop floors, band widths, displacement sizes)
until a valid, cost-floor-clean signal fires.

WHAT IS NEVER RELAXED (the rails stay rails):
  - loss caps, attempt budgets, streak cooldowns      (engine/risk.py)
  - black-swan sentinel, news blackout, spread guard   (safety stops)
  - cost-floor geometry: risk_floor_atr / risk_floor_pct
    (a stop closer than fees+slippage is mathematically untradeable,
     assurance never sizes into negative expectancy)
  - trend-bias filter: never fight the D1 EMA20 tape
  - fail-closed indicator gate: NaN stays HOLD

Every relaxed signal is tagged "ASSURE-L{step}" in its reason so the
journal can audit exactly which escalation level traded.

Ladder (per step, cumulative):
  L1  ADX floors x0.5 · ATR dead-tape floor x0.75 · M2 bands +4/-4
  L2  ADX floors 0 · ATR floor x0.5 · pullback zone x2 · sweep wick
      x0.6 · NY body x0.6 · M2 bands +4/-4 again
  L3  RSI bands wide-open (M1/M2) · BB position full range · asian
      range sanity halved - on a literally dead flat tape M2 mean-revert
      becomes always-eligible (still cost-floor gated, still $1 risk)
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from engine.config import Config


def level(now: datetime | None,
          day_attempts: int,
          has_pending: bool,
          cfg: Config) -> int:
    """Assurance escalation level for this moment: 0 = off, 1..steps.

    Off when: feature disabled, before the assure hour, the day already
    produced a fill, or an order is still working the book.
    """
    if not cfg.assure_daily or cfg.assure_steps <= 0:
        return 0
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if now.hour < cfg.assure_hour_utc:
        return 0
    if day_attempts > 0 or has_pending:
        return 0
    mins_past = (now.hour - cfg.assure_hour_utc) * 60 + now.minute
    step = 1 + min(mins_past // max(cfg.assure_escalate_min, 1),
                   cfg.assure_steps - 1)
    return max(1, step)


def relaxed_config(cfg: Config, step: int) -> Config:
    """A RELAXED COPY of the config for escalation `step` (1-based).
    The original config object is never mutated."""
    k = max(0, min(int(step), 3))
    if k <= 0:
        return cfg
    c = replace(cfg)
    if k >= 1:
        c.m1_min_adx = cfg.m1_min_adx * 0.5
        c.m4_min_adx = cfg.m4_min_adx * 0.5
        c.min_atr_usd = cfg.min_atr_usd * 0.75
        c.m2_rsi_lo = cfg.m2_rsi_lo - 4.0
        c.m2_rsi_hi = cfg.m2_rsi_hi + 4.0
        c.m2_bb_lo = cfg.m2_bb_lo * 0.5
        c.m2_bb_hi = 1.0 - (1.0 - cfg.m2_bb_hi) * 0.5
    if k >= 2:
        c.m1_min_adx = 0.0
        c.m4_min_adx = 0.0
        c.min_atr_usd = cfg.min_atr_usd * 0.5
        c.m1_near_atr = cfg.m1_near_atr * 2.0
        c.m3_min_wick_atr = cfg.m3_min_wick_atr * 0.6
        c.m4_body_atr = cfg.m4_body_atr * 0.6
        c.m2_rsi_lo = cfg.m2_rsi_lo - 8.0
        c.m2_rsi_hi = cfg.m2_rsi_hi + 8.0
    if k >= 3:
        c.m1_rsi_long = (20.0, 75.0)
        c.m1_rsi_short = (25.0, 80.0)
        c.m2_rsi_lo = 35.0
        c.m2_rsi_hi = 65.0
        c.m2_bb_lo = 0.0
        c.m2_bb_hi = 1.0
        c.m2_max_adx = cfg.m2_max_adx + 6.0
        c.m3_min_range = cfg.m3_min_range * 0.5
        c.m3_max_range = cfg.m3_max_range * 2.0
    return c
