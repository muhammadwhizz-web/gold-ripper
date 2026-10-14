#!/usr/bin/env python3
"""
GOLD RIPPER :: Lookahead Self-Check
===================================
freqtrade lookahead-analysis merge (optimize/analysis/lookahead.py idea):
re-run the indicator context with data TRUNCATED at a decision time and
assert the values the strategy would have seen are IDENTICAL to the ones
computed on the full frame. Any resample/shift/ffill leak shows up here
as a mismatch. Run:
    python scripts/test_lookahead.py
Exit 0 = no leak.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from backtest import WARMUP_BARS  # noqa: E402
from engine.config import Config  # noqa: E402
from engine.strategy import StrategyEngine  # noqa: E402

H1_PATH = ROOT / "data" / "history" / "gold_1h.csv"

KEYS = ["ema_f", "ema_s", "ema_p", "rsi", "adx", "atr", "bbp",
        "d1_ema20", "h4_fast", "h4_slow", "dc_hi", "dc_lo"]


def main() -> int:
    if not H1_PATH.exists():
        print(f"[!] no tape at {H1_PATH} - run data/fetch_history.py first")
        return 2
    df = pd.read_csv(H1_PATH, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    cfg = Config()
    eng = StrategyEngine(cfg)
    full_ctx = eng._context(df)

    # decision point: some bar after warmup
    probe = max(WARMUP_BARS + 50, len(df) // 2)
    ts = df.index[probe]
    truncated = df.loc[:ts - pd.Timedelta(seconds=1)]   # strictly BEFORE ts
    trunc_ctx = eng._context(truncated)

    bad = []
    for k in KEYS:
        a = full_ctx[k].iloc[probe]
        b = trunc_ctx[k].iloc[len(trunc_ctx[k]) - 1]
        # the last CLOSED bar before ts must match exactly in both frames
        a = full_ctx[k].loc[:ts - pd.Timedelta(seconds=1)].iloc[-1]
        if pd.isna(a) and pd.isna(b):
            continue
        try:
            same = abs(float(a) - float(b)) <= 1e-9
        except (TypeError, ValueError):
            same = str(a) == str(b)
        if not same:
            bad.append((k, float(a), float(b)))

    if bad:
        print("[!] LOOKAHEAD LEAK detected:")
        for k, a, b in bad:
            print(f"    {k}: full={a} truncated={b}")
        return 1
    n_checked = len(KEYS)
    print(f"[+] no lookahead leak: {n_checked} indicator series identical "
          f"at {ts} (full frame vs truncated-at-decision frame)")
    print("[+] resample/shift/ffill HTF merge is leak-safe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
