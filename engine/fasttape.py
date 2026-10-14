"""
GOLD RIPPER :: FastTape
=======================
Millisecond-class INCREMENTAL indicator engine (the "work in milliseconds"
requirement). The pandas context (engine/strategy.py _context) recomputes
full series every bar - correct for calibration, overkill for the live
loop. FastTape ingests ONE closed bar at a time and updates every
indicator in O(1):

  EMA(fast/slow/pull)   scalar state, one multiply-add per bar
  RSI(14)               Wilder gains/losses
  ATR(14)               bar-range rolling mean (same estimator as pandas!)
  ADX(14)               Wilder +DM/-DM/TR
  Bollinger position    running sums ring buffer (n=20, k=2)
  Donchian(24)          monotonic deque of (idx, price) - O(1) amortized
  Session               UTC hour buckets

Design rules:
  - CLOSED BARS ONLY: push() once per bar close; the forming bar never
    touches the state (no EMA corruption, identical numbers to pandas).
  - fail-closed: before the first warmup completes, snapshot() reports
    ready=False and the ripper does not act on it.
  - every push() and snapshot() self-times; the latency rides the
    heartbeat ("fast.us") so the ms claim is measured, not promised.

Bench: scripts/bench_fasttape.py -> per-bar push in microseconds.
"""
from __future__ import annotations

import math
import time
from collections import deque

from engine.indicators import session_of


class FastTape:
    def __init__(self, ema_fast: int = 50, ema_slow: int = 200,
                 ema_pull: int = 20, rsi_n: int = 14, atr_n: int = 14,
                 adx_n: int = 14, bb_n: int = 20, bb_k: float = 2.0,
                 dc_n: int = 24) -> None:
        self.ema_fast_n = max(2, ema_fast)
        self.ema_slow_n = max(2, ema_slow)
        self.ema_pull_n = max(2, ema_pull)
        self.rsi_n = max(2, rsi_n)
        self.atr_n = max(2, atr_n)
        self.adx_n = max(2, adx_n)
        self.bb_n = max(2, bb_n)
        self.bb_k = float(bb_k)
        self.dc_n = max(2, dc_n)

        # ema scalars (seeded on first bar = price, like ewm adjust=False)
        self._ef = self._es = self._ep = None
        # rsi (wilder)
        self._rsi = 50.0
        self._ag = self._al = None
        # atr: bar-range rolling mean (identical to engine.indicators.atr)
        self._atr_rng = deque(maxlen=self.atr_n)
        # adx (wilder)
        self._atr_w = self._pdm_w = self._mdm_w = None
        self._adx: float | None = None
        self._prev_h = self._prev_l = self._prev_c = None
        # bollinger running sums
        self._bb_q = deque(maxlen=self.bb_n)
        self._bb_sum = 0.0
        self._bb_sq = 0.0
        # donchian monotonic deques (idx, value)
        self._dcq_hi: deque = deque()
        self._dcq_lo: deque = deque()
        self._bar = 0

        self.last_ts = None
        self.px = None
        # latency telemetry
        self.us_push = 0.0      # ewma of push() cost, microseconds
        self.us_snap = 0.0      # ewma of snapshot() cost, microseconds
        self.n_push = 0

    # ------------------------------------------------------------ ingest
    def push(self, o: float, h: float, l: float, c: float, ts=None) -> None:
        t0 = time.perf_counter_ns()
        k_f = 2.0 / (self.ema_fast_n + 1)
        k_s = 2.0 / (self.ema_slow_n + 1)
        k_p = 2.0 / (self.ema_pull_n + 1)
        self._ef = c if self._ef is None else c * k_f + self._ef * (1 - k_f)
        self._es = c if self._es is None else c * k_s + self._es * (1 - k_s)
        self._ep = c if self._ep is None else c * k_p + self._ep * (1 - k_p)

        # rsi (wilder)
        d = c - (self._prev_c if self._prev_c is not None else c)
        g, ls = max(d, 0.0), max(-d, 0.0)
        a = 1.0 / self.rsi_n
        self._ag = g if self._ag is None else (a * g + (1 - a) * self._ag)
        self._al = ls if self._al is None else (a * ls + (1 - a) * self._al)
        if self._ag is not None and self._al is not None:
            self._rsi = 50.0 if (self._ag == 0 and self._al == 0) else (
                100.0 - 100.0 / (1.0 + self._ag / self._al)
                if self._al > 0 else 100.0)

        # atr: bar range rolling mean (NO true-range gaps - matches pandas)
        self._atr_rng.append(h - l)

        # adx (wilder smoothing)
        if self._prev_h is not None:
            up = h - self._prev_h
            dn = self._prev_l - l
            pdm = up if (up > dn and up > 0) else 0.0
            mdm = dn if (dn > up and dn > 0) else 0.0
            tr = max(h - l, abs(h - self._prev_c), abs(l - self._prev_c))
            aw = 1.0 / self.adx_n
            self._atr_w = tr if self._atr_w is None else \
                (aw * tr + (1 - aw) * self._atr_w)
            self._pdm_w = pdm if self._pdm_w is None else \
                (aw * pdm + (1 - aw) * self._pdm_w)
            self._mdm_w = mdm if self._mdm_w is None else \
                (aw * mdm + (1 - aw) * self._mdm_w)
            if self._atr_w and self._atr_w > 0:
                pdi = 100.0 * self._pdm_w / self._atr_w
                mdi = 100.0 * self._mdm_w / self._atr_w
                s = pdi + mdi
                dx = 100.0 * abs(pdi - mdi) / s if s > 0 else 0.0
                self._adx = dx if self._adx is None else \
                    (dx * aw + self._adx * (1 - aw))
        self._prev_h, self._prev_l, self._prev_c = h, l, c

        # bollinger running sums (SAMPLE std, n-1 - identical to pandas .std());
        # evict the rolling buffer's oldest value BEFORE appending
        if len(self._bb_q) == self.bb_n:
            old = self._bb_q[0]
            self._bb_sum -= old
            self._bb_sq -= old * old
        self._bb_q.append(c)
        self._bb_sum += c
        self._bb_sq += c * c
        if len(self._bb_q) == self.bb_n:
            n = self.bb_n
            mean = self._bb_sum / n
            var = max(0.0, (self._bb_sq - n * mean * mean) / (n - 1))
            self._bb_mean, self._bb_sd = mean, math.sqrt(var)

        # donchian monotonic deques over HIGH/LOW
        while self._dcq_hi and self._dcq_hi[-1][1] <= h:
            self._dcq_hi.pop()
        self._dcq_hi.append((self._bar, h))
        while self._dcq_lo and self._dcq_lo[-1][1] >= l:
            self._dcq_lo.pop()
        self._dcq_lo.append((self._bar, l))
        cutoff = self._bar - self.dc_n
        while self._dcq_hi and self._dcq_hi[0][0] <= cutoff:
            self._dcq_hi.popleft()
        while self._dcq_lo and self._dcq_lo[0][0] <= cutoff:
            self._dcq_lo.popleft()
        self._bar += 1
        self.px = c
        self.last_ts = ts or self.last_ts
        self.n_push += 1
        dt = (time.perf_counter_ns() - t0) / 1000.0
        self.us_push = dt if self.us_push == 0.0 else \
            0.2 * dt + 0.8 * self.us_push

    def warmup(self, df) -> int:
        """Seed from a pandas OHLCV frame (closed bars only)."""
        n = 0
        for ts, row in df.iterrows():
            self.push(float(row["open"]), float(row["high"]),
                      float(row["low"]), float(row["close"]), ts=ts)
            n += 1
        return n

    # ------------------------------------------------------------ read
    def ready(self) -> bool:
        return (self.n_push >= max(self.ema_slow_n, self.bb_n,
                                   self.dc_n + 1, self.atr_n))

    def snapshot(self) -> dict:
        """Latest indicator values + latency, in microseconds."""
        t0 = time.perf_counter_ns()
        dc_hi = self._dcq_hi[0][1] if self._dcq_hi else None
        dc_lo = self._dcq_lo[0][1] if self._dcq_lo else None
        atr = (sum(self._atr_rng) / len(self._atr_rng)) if self._atr_rng \
            else None
        out = {
            "ready": self.ready(),
            "bars": self.n_push,
            "px": self.px,
            "ema_f": self._ef, "ema_s": self._es, "ema_p": self._ep,
            "rsi": self._rsi,
            "adx": (self._adx if self._adx is not None else 0.0),
            "atr": atr,
            "bbp": (None if not self.ready() else
                    max(0.0, min(1.0, (self.px - (self._bb_mean -
                        self.bb_k * self._bb_sd)) /
                        max(1e-9, 2 * self.bb_k * self._bb_sd)))),
            "dc_hi": dc_hi, "dc_lo": dc_lo,
            "session": (session_of(self.last_ts)
                        if self.last_ts is not None else None),
            "us_push": round(self.us_push, 1),
            "us_snap": round(self.us_snap, 1),
        }
        dt = (time.perf_counter_ns() - t0) / 1000.0
        self.us_snap = dt if self.us_snap == 0.0 else \
            0.2 * dt + 0.8 * self.us_snap
        out["us_snap"] = round(self.us_snap, 1)
        return out

    def json_safe(self) -> dict:
        s = self.snapshot()
        return {k: (round(v, 4) if isinstance(v, float) and math.isfinite(v)
                    else v) for k, v in s.items()}
