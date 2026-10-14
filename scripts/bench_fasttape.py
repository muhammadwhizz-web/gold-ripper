#!/usr/bin/env python3
"""
GOLD RIPPER :: FastTape micro-benchmark
=======================================
The "works in milliseconds" proof: per-bar push and per-read snapshot
latency of the incremental indicator engine, measured directly.
    python scripts/bench_fasttape.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from engine.fasttape import FastTape  # noqa: E402


def main() -> int:
    n = 200_000
    rng = np.random.default_rng(11)
    px = 2000.0 + np.cumsum(rng.normal(0, 1.0, n))
    hi = px + np.abs(rng.normal(0, 0.7, n))
    lo = px - np.abs(rng.normal(0, 0.7, n))
    op = np.roll(px, 1)
    op[0] = px[0]

    ft = FastTape()
    t0 = time.perf_counter()
    for i in range(n):
        ft.push(float(op[i]), float(hi[i]), float(lo[i]), float(px[i]))
    total = time.perf_counter() - t0
    us_push = total / n * 1e6

    t0 = time.perf_counter()
    reads = 10_000
    for _ in range(reads):
        s = ft.snapshot()
    us_snap = (time.perf_counter() - t0) / reads * 1e6

    print("=== GOLD RIPPER FastTape micro-benchmark ===")
    print(f"  bars pushed        : {n:,}")
    print(f"  push latency       : {us_push:8.2f} us/bar  "
          f"({us_push / 1000:.4f} ms)")
    print(f"  snapshot latency   : {us_snap:8.2f} us/read "
          f"({us_snap / 1000:.4f} ms)")
    print(f"  bars/second        : {1e6 / us_push:,.0f}")
    print(f"  self-reported push : {ft.us_push:.2f} us (ewma)")
    print(f"  self-reported snap : {ft.us_snap:.2f} us (ewma)")
    print(f"  ready              : {s['ready']}")
    ok = us_push < 1000 and us_snap < 1000
    print(f"  ms-class claim     : {'PROVEN' if ok else 'FAILED'} "
          f"(both well under 1 ms)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
