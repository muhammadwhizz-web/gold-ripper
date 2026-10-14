"""
GOLD RIPPER :: Pre-Trade Guards
===============================
Merged from the 9-repo audit (Task 2, 2026 round):

  - astra-quant-agent  order_submit.py:341-358  -> price-cross hallucination
    guard (a stale limit on the wrong side of spot = instant wrong fill)
  - astra-quant-agent  order_risk.py:16-89      -> R:R geometry validator +
    EV gate (the "TP1 below stop" ladder bug that costs 0.6R per trade)
  - astra-quant-agent  circuit_breaker.py:272   -> black-swan sentinel
    (a >=3xATR single bar blocks ALL new entries - never average into a quake)
  - astra-quant-agent  signals.py:73-80,263     -> fail-closed missing
    indicators (NaN never satisfies an entry) + ATR-shock filter
  - freqtrade          SpreadFilter.py          -> pre-entry spread guard
  - astra-quant-agent  risk_constants.py:100    -> ladder calibration assert:
    TP1 trigger R must be >= the stop's R multiple (win-small-lose-big trap)

Every guard is pure math on numbers you already have - no network calls,
microsecond cost, fail-CLOSED (any doubt = no trade).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

# price-cross guard: a limit order may not cross spot by more than this
PRICE_CROSS_MAX_DEV = 0.005     # 0.5% (astra-quant default)
# spread guard default (freqtrade SpreadFilter spirit, tuned for XAU perp)
MAX_SPREAD_BPS = 8.0
# black-swan sentinel: a bar that moves >= SWAN_ATR_MULT x ATR (or >= SWAN_ABS
# USD) kills all new entries until the tape calms (SWAN_COOLDOWN_BARS)
SWAN_ATR_MULT = 3.0
SWAN_ABS_USD = 30.0
SWAN_COOLDOWN_BARS = 3
# shock filter: ATR exploding vs its own median suppresses breakout entries
SHOCK_ATR_MULT = 2.5


def _finite(*vals: float) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals)


# ───────────────────────────────────────────────────── geometry validation
def validate_geometry(side: str, entry: float, sl: float, tp1: float,
                      tp1_r: float, min_tp1_r: float = 1.0) -> tuple[bool, str]:
    """R:R geometry + ladder-calibration assert (astra-quant order_risk.py).

    Rules:
      sl < entry < tp1 (LONG)  /  tp1 < entry < sl (SHORT)
      all values finite, risk distance > 0
      tp1_r >= min_tp1_r - the astra-quant live lesson (TP1 1.2xATR against
      a 2.0xATR stop locks in win-small-lose-big: their measured avg win
      23.9 vs avg loss 48.0). Structural checks (entry path) use a loose
      floor; train.py hard-DQs any candidate below 1.0.
    """
    if not _finite(entry, sl, tp1):
        return False, f"non-finite geometry entry={entry} sl={sl} tp1={tp1}"
    if min_tp1_r > 0 and not _finite(tp1_r):
        return False, f"non-finite tp1_r {tp1_r} refused (strict mode)"
    if _finite(tp1_r) and tp1_r < min_tp1_r:
        return False, f"ladder calibration: tp1_r {tp1_r} < {min_tp1_r} refused"
    risk = (entry - sl) if side == "LONG" else (sl - entry)
    reward = (tp1 - entry) if side == "LONG" else (entry - tp1)
    if risk <= 0:
        return False, f"stop on the wrong side of entry ({side} risk {risk:.2f})"
    if reward <= 0:
        return False, f"TP1 on the wrong side of entry ({side} reward {reward:.2f})"
    return True, "OK"


# ───────────────────────────────────────────── price-cross hallucination
def price_cross_ok(side: str, limit_px: float, spot: float,
                   max_dev: float = PRICE_CROSS_MAX_DEV) -> tuple[bool, str]:
    """Reject maker limits that cross (or absurdly over-reach) spot.

    astra-quant order_submit.py:341-358 verbatim idea:
      BUY  limit > spot*(1+0.5%)  -> instant wrong fill -> refuse
      SELL limit < spot*(1-0.5%)  -> same
      anything > 50% from spot    -> fat-finger -> refuse
    Returns (ok, reason). The caller falls back to the taker path.
    """
    if not _finite(limit_px, spot) or spot <= 0 or limit_px <= 0:
        return False, "non-finite price/spot"
    if limit_px > spot * 1.5 or limit_px < spot * 0.5:
        return False, f"limit {limit_px:.2f} >50% from spot {spot:.2f}"
    if side == "LONG" and limit_px > spot * (1 + max_dev):
        return False, (f"BUY limit {limit_px:.2f} crosses spot "
                       f"{spot:.2f} (+{max_dev * 100:.1f}% cap)")
    if side == "SHORT" and limit_px < spot * (1 - max_dev):
        return False, (f"SELL limit {limit_px:.2f} crosses spot "
                       f"{spot:.2f} (-{max_dev * 100:.1f}% cap)")
    return True, "OK"


# ───────────────────────────────────────────────────────── spread guard
def spread_ok(bid: float | None, ask: float | None,
              max_bps: float = MAX_SPREAD_BPS) -> tuple[bool, str]:
    """freqtrade SpreadFilter: a wide book is a news/rollover tell."""
    if bid is None or ask is None or not _finite(bid, ask):
        return True, "no book - skip spread check"   # fail-open (paper mode)
    if bid <= 0 or ask <= 0 or ask < bid:
        return False, f"broken book bid={bid} ask={ask}"
    bps = (ask - bid) / ((ask + bid) / 2) * 10_000
    if bps > max_bps:
        return False, f"spread {bps:.1f}bps > {max_bps:.1f}bps cap"
    return True, "OK"


# ────────────────────────────────────────────────────── black-swan sentinel
def black_swan(df: pd.DataFrame, i: int, atr_now: float,
               mult: float = SWAN_ATR_MULT,
               abs_usd: float = SWAN_ABS_USD) -> tuple[bool, str]:
    """True while the tape is in quake mode (astra-quant circuit_breaker).

    A single bar whose close-to-close move exceeds mult x ATR or abs_usd
    blocks NEW entries for the next few bars. Exits/stops stay live.
    """
    if i < 1:
        return False, ""
    lookback = min(len(df) - 1, 1 + SWAN_COOLDOWN_BARS)
    c = df["close"].iloc[max(0, i - lookback):i + 1]
    if len(c) < 2:
        return False, ""
    jump = float((c.diff().abs().iloc[-1]))
    if atr_now > 0 and jump >= mult * atr_now:
        return True, f"quake bar {jump:.2f} >= {mult:.1f}xATR {atr_now:.2f}"
    if jump >= abs_usd:
        return True, f"quake bar {jump:.2f} >= ${abs_usd:.0f}"
    return False, ""


# ─────────────────────────────────────────────────────────── shock filter
def shock(atr_series: pd.Series, i: int,
          max_x: float = SHOCK_ATR_MULT) -> tuple[bool, str]:
    """ATR% explosion vs its own median -> suppress breakout/trend entries."""
    a = float(atr_series.iloc[i])
    if not math.isfinite(a) or a <= 0:
        return True, "ATR unavailable - shock assumes ON (fail-closed)"
    med = float(atr_series.iloc[max(0, i - 100):i + 1].median())
    if med > 0 and a >= max_x * med:
        return True, f"ATR shock {a:.2f} >= {max_x}x median {med:.2f}"
    return False, ""


# ────────────────────────────────────────────────── fail-closed indicators
def indicators_ready(row: dict) -> tuple[bool, str]:
    """NaN never satisfies an entry (astra-quant signals.py:73-80).

    Pass the indicator snapshot the module is about to trade on; any NaN
    or inf vetoes the signal. Missing data = HOLD, never 'trade anyway'.
    """
    for k, v in row.items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return False, f"indicator {k} not numeric ({v!r})"
        if not math.isfinite(fv):
            return False, f"indicator {k} is {v!r} - fail-closed"
    return True, "OK"
