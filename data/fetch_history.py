"""
GOLD RIPPER :: History Fetcher
==============================
Downloads real market history for local training + live operation.
  - GC=F (COMEX gold futures) daily  -> full available history (~26 years)
  - GC=F hourly                      -> last ~730 days (Yahoo limit)
  - GC=F 15m                         -> last 60 days (Yahoo limit)

Everything is stored locally as CSV in data/history/. The trainer
(train.py) and the live bot (ripper.py) both read these files.

Multi-symbol:
  python data/fetch_history.py --tag XAG --yf-symbol SI=F
writes {tag}_daily.csv / {tag}_1h.csv / {tag}_15m.csv so a second ripper
instance can be trained + run on silver (or any Yahoo-listed tape).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
HIST = ROOT / "data" / "history"
SYMBOL = "GC=F"          # COMEX gold futures front month - the gold benchmark
TAG = "gold"


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten possible MultiIndex columns, normalize schema."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df = df[~df.index.duplicated(keep="last")].dropna(subset=["close"])
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


def fetch(interval: str, period: str, symbol: str | None = None) -> pd.DataFrame:
    import yfinance as yf
    raw = yf.download(symbol or SYMBOL, period=period, interval=interval,
                      progress=False, auto_adjust=True)
    return _clean(raw)


def main() -> int:
    global SYMBOL, TAG
    ap = argparse.ArgumentParser(description="GOLD RIPPER history fetcher")
    ap.add_argument("--yf-symbol", default="GC=F",
                    help="Yahoo Finance ticker (GC=F gold, SI=F silver, ...)")
    ap.add_argument("--tag", default="gold",
                    help="file tag: writes {tag}_1h.csv etc.")
    a = ap.parse_args()
    SYMBOL, TAG = a.yf_symbol, a.tag.lower()

    HIST.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("1d", "max", f"{TAG}_daily.csv"),      # full history - the recon base
        ("1h", "730d", f"{TAG}_1h.csv"),        # ~2.4 years - main training tape
        ("15m", "60d", f"{TAG}_15m.csv"),       # microstructure check
    ]
    ok = 0
    for interval, period, fname in jobs:
        try:
            df = fetch(interval, period)
            if len(df) < 50:
                print(f"[!] {fname}: only {len(df)} rows - skipped")
                continue
            df.to_csv(HIST / fname)
            print(f"[+] {fname:<15} {len(df):>7} bars  "
                  f"{df.index.min()} -> {df.index.max()}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"[!] {fname} failed: {e}")
    if ok == 0:
        print("[!] no data fetched - check network", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
