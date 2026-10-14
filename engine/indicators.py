"""
GOLD RIPPER :: Indicators
=========================
Pure pandas/numpy implementations - no external TA library needed.
Every function returns a pd.Series aligned to the input index.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=max(2, int(n)), adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(max(2, int(n))).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / max(1, n), adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / max(1, n), adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Bar-range ATR: simple rolling mean of high-low.

    Deliberately NOT true-range-with-gaps: overnight futures gaps would
    inflate stop distances 3-6x on this tape and destroy the calibrated
    geometry. Backtest and live use the SAME estimator, so the gap risk
    is handled by the TP1->breakeven ladder instead of by inflated stops.
    """
    return (df["high"] - df["low"]).rolling(max(2, int(n))).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()],
                   axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1 / n, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(
        alpha=1 / n, adjust=False).mean() / atr_
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(
        alpha=1 / n, adjust=False).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0.0)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, n)
    sd = close.rolling(n).std()
    return mid - k * sd, mid, mid + k * sd


def bb_pos(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    lo, mid, hi = bollinger(close, n, k)
    width = (hi - lo).replace(0, np.nan)
    return ((close - lo) / width).clip(0, 1)


def donchian(df: pd.DataFrame, n: int = 20) -> tuple[pd.Series, pd.Series]:
    hi = df["high"].rolling(n).max().shift(1)
    lo = df["low"].rolling(n).min().shift(1)
    return hi, lo


def swings(df: pd.DataFrame, lookback: int = 5) -> tuple[pd.Series, pd.Series]:
    """Rolling swing high/low (fractal-free, simple rolling extreme)."""
    sh = df["high"].rolling(lookback).max().shift(1)
    sl = df["low"].rolling(lookback).min().shift(1)
    return sh, sl


def session_of(ts) -> str:
    h = ts.hour
    if 0 <= h < 7:
        return "ASIA"
    if 7 <= h < 12:
        return "LONDON"
    if 12 <= h < 16:
        return "OVERLAP"
    if 16 <= h < 21:
        return "NEWYORK"
    return "LATE_US"
