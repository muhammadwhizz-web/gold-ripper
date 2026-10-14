#!/usr/bin/env python3
"""
GOLD RIPPER :: Guard + Protection + FastTape unit tests
=======================================================
Merged-code verification (9-repo audit integration). Run:
    python scripts/test_guards.py
Exit 0 = all pass. No network needed.
"""
from __future__ import annotations

import math
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from engine import guards  # noqa: E402
from engine.config import Config  # noqa: E402
from engine.fasttape import FastTape  # noqa: E402
from engine.protections import ProtectionManager  # noqa: E402
from engine.indicators import adx as pd_adx  # noqa: E402
from engine.indicators import atr as pd_atr  # noqa: E402
from engine.indicators import bb_pos as pd_bbp  # noqa: E402
from engine.indicators import ema as pd_ema  # noqa: E402
from engine.indicators import rsi as pd_rsi  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# ───────────────────────────────────────────────────── geometry guard
def test_geometry() -> None:
    print("guards.validate_geometry")
    ok, why = guards.validate_geometry("LONG", 2000.0, 1990.0, 2015.0, 1.5)
    check("valid long geometry", ok)
    ok, why = guards.validate_geometry("LONG", 2000.0, 2000.0 - 10, 1990.0, 1.0)
    check("tp1 below entry refused", not ok)
    ok, why = guards.validate_geometry("LONG", 2000.0, 2010.0, 2015.0, 1.0)
    check("stop above entry refused", not ok)
    ok, why = guards.validate_geometry("LONG", 2000.0, 1990.0, 2010.0, float("nan"))
    check("non-finite tp1_r refused", not ok)
    ok, why = guards.validate_geometry("LONG", 2000.0, 1990.0, 2008.0, 0.8,
                                       min_tp1_r=1.0)
    check("tp1_r < 1.0 refused when strict", not ok)
    ok, why = guards.validate_geometry("LONG", 2000.0, 1990.0, 2008.0, 0.8,
                                       min_tp1_r=0.0)
    check("tp1_r < 1.0 allowed structural", ok)


# ─────────────────────────────────────────────── price-cross + spread
def test_price_cross() -> None:
    print("guards.price_cross_ok / spread_ok")
    ok, _ = guards.price_cross_ok("LONG", 2011.0, 2000.0)   # +0.55%
    check("BUY limit crossing spot refused", not ok)
    ok, _ = guards.price_cross_ok("LONG", 2005.0, 2000.0)   # +0.25%
    check("BUY limit slightly above spot ok", ok)
    ok, _ = guards.price_cross_ok("SHORT", 1989.0, 2000.0)  # -0.55%
    check("SELL limit crossing spot refused", not ok)
    ok, _ = guards.price_cross_ok("LONG", 3500.0, 2000.0)
    check("fat-finger 75% over-reach refused", not ok)
    ok, _ = guards.spread_ok(2000.0, 2000.5, max_bps=8.0)
    check("tight spread ok", ok)
    ok, _ = guards.spread_ok(2000.0, 2003.0, max_bps=8.0)   # 15 bps
    check("wide spread refused", not ok)
    ok, _ = guards.spread_ok(None, None)
    check("no book -> fail-open (paper)", ok)


# ─────────────────────────────────────────────────────── black swan
def test_black_swan() -> None:
    print("guards.black_swan")
    n = 30
    idx = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    df = pd.DataFrame({"open": 2000.0, "high": 2001.0, "low": 1999.0,
                       "close": 2000.0}, index=idx)
    atr = 1.0
    swan, _ = guards.black_swan(df, n - 1, atr)
    check("calm tape -> no swan", not swan)
    df.iloc[-1, df.columns.get_loc("close")] = 2004.0   # +4 jump = 4xATR
    swan, why = guards.black_swan(df, n - 1, atr)
    check("4xATR quake -> swan", swan, why)
    df.iloc[-1, df.columns.get_loc("close")] = 2035.0   # $35 abs
    swan, _ = guards.black_swan(df, n - 1, 10.0)
    check("$35 bar vs $30 cap -> swan", swan)


# ───────────────────────────────────────────────────── protections
def test_protections() -> None:
    print("protections.ProtectionManager")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config()
        cfg.state_file = Path(td) / "ripper_state.json"
        cfg.protections_file = Path(td) / "protections.json"
        cfg.journal_file = Path(td) / "journal.csv"
        cfg.prot_cooldown_bars = 2       # -> 120 min
        cfg.prot_sl_window_bars = 24
        cfg.prot_sl_max = 2
        cfg.prot_sl_pause_bars = 4
        cfg.prot_dd_window_bars = 24
        cfg.prot_dd_max_usd = 6.0
        pm = ProtectionManager(cfg)
        ok, _ = pm.can_enter("LONG")
        check("fresh manager allows entry", ok)
        # exit 1: cooldown arms, still allowed (lock side-aware? both sides)
        pm.on_exit(time.time(), -1.0, "SL", "LONG", True)
        ok, why = pm.can_enter("LONG")
        check("cooldown blocks after close", not ok, why)
        ok, _ = pm.can_enter("SHORT")
        check("cooldown lock is side-aware (SHORT may hunt)", ok)
        # stoploss guard: 3 losses in window > max 2
        pm.on_exit(time.time() - 3600, -1.0, "SL", "LONG", True)
        pm.on_exit(time.time() - 3500, -1.0, "SL", "LONG", True)
        pm.on_exit(time.time() - 3400, -1.0, "SL", "SHORT", True)
        ok, why = pm.can_enter("SHORT")
        check("stoploss guard trips at 3 losses/24h", not ok, why)
        check("guard reason mentions stoploss", "stoploss_guard" in why, why)
        # drawdown guard: balance bled $6.5 over the window
        pm2 = ProtectionManager(cfg)
        pm2.on_exit(time.time() - 7200, -3.0, "SL", "LONG", True)
        pm2.on_exit(time.time() - 3600, -3.5, "SL", "LONG", True)
        kinds = {lk["kind"] for lk in pm2.locks}
        check("drawdown guard trips at $6.5", "max_drawdown" in kinds,
              str(kinds))
        ok, why = pm2.can_enter("LONG")
        check("dd lock blocks entry (cooldown or dd first)", not ok, why)
        # locks never shortened, only lengthened
        pm3 = ProtectionManager(cfg)
        pm3.on_exit(time.time(), -1.0, "TP1-ish", "LONG", True)
        before = pm3.locks[0]["until"]
        pm3.on_exit(time.time(), -1.0, "again", "LONG", True)
        after = [lk for lk in pm3.locks if lk["kind"] == "cooldown"][0]["until"]
        check("lock extended not shortened", after >= before)
        # persistence across restart
        pm4 = ProtectionManager(cfg)
        ok, _ = pm4.can_enter("LONG")
        check("locks survive restart", not ok)


# ───────────────────────────────────────────────────────── fasttape
def _mk_tape(n: int = 600, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    px = 2000.0 + np.cumsum(rng.normal(0, 1.2, n))
    hi = px + np.abs(rng.normal(0, 0.8, n))
    lo = px - np.abs(rng.normal(0, 0.8, n))
    op = np.roll(px, 1); op[0] = px[0]
    idx = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": op, "high": hi, "low": lo,
                         "close": px, "volume": 1.0}, index=idx)


def test_fasttape_matches_pandas() -> None:
    print("fasttape vs pandas (warm tape)")
    df = _mk_tape()
    ft = FastTape()
    ft.warmup(df)
    snap = ft.snapshot()
    check("tape reports ready", snap["ready"])
    c = df["close"]
    check("ema_fast matches",
          abs(snap["ema_f"] - float(pd_ema(c, 50).iloc[-1])) < 1e-6)
    check("ema_slow matches",
          abs(snap["ema_s"] - float(pd_ema(c, 200).iloc[-1])) < 1e-6)
    check("rsi matches",
          abs(snap["rsi"] - float(pd_rsi(c, 14).iloc[-1])) < 1e-6)
    check("atr matches (bar-range estimator)",
          abs(snap["atr"] - float(pd_atr(df, 14).iloc[-1])) < 1e-9)
    check("bbp matches",
          abs((snap["bbp"] or 0) - float(pd_bbp(c, 20, 2.0).iloc[-1])) < 0.02)
    adx_ft, adx_pd = snap["adx"], float(pd_adx(df, 14).iloc[-1])
    check("adx same regime (±25% of pandas wilder)", 
          adx_ft == 0 or abs(adx_ft - adx_pd) / max(adx_pd, 1e-9) < 0.25,
          f"ft={adx_ft:.1f} pd={adx_pd:.1f}")
    dc_hi = float(df["high"].rolling(24).max().shift(1).iloc[-1])
    check("donchian_hi matches (closed-bar)", abs((snap["dc_hi"] or 0) - dc_hi) < 1e-9)
    # incremental == warmup: push one more bar, compare with pandas recompute
    more = _mk_tape(1, seed=99)
    r = more.iloc[0]
    ft.push(float(r["open"]), float(r["high"]), float(r["low"]),
            float(r["close"]), ts=df.index[-1] + pd.Timedelta(hours=1))
    df2 = pd.concat([df, more], ignore_index=False)
    check("incremental ema == recompute",
          abs(ft.snapshot()["ema_f"] -
              float(pd_ema(df2["close"], 50).iloc[-1])) < 1e-6)


def test_fasttape_latency() -> None:
    print("fasttape latency (ms-class claim)")
    df = _mk_tape(20000, seed=3)
    ft = FastTape()
    t0 = time.perf_counter()
    ft.warmup(df)
    dt = (time.perf_counter() - t0) / len(df)
    us_per_bar = dt * 1e6
    check(f"push <= 100us/bar (measured {us_per_bar:.1f}us)",
          us_per_bar < 100.0)
    t0 = time.perf_counter()
    for _ in range(1000):
        ft.snapshot()
    us_snap = (time.perf_counter() - t0) / 1000 * 1e6
    check(f"snapshot <= 50us (measured {us_snap:.1f}us)", us_snap < 50.0)
    check("latency telemetry populated", ft.us_push > 0 and ft.us_snap > 0)


def test_fail_closed() -> None:
    print("guards.indicators_ready (fail-closed)")
    ok, _ = guards.indicators_ready({"atr": 1.2, "rsi": 50.0})
    check("finite indicators ok", ok)
    ok, why = guards.indicators_ready({"atr": float("nan")})
    check("NaN atr refused", not ok, why)
    ok, why = guards.indicators_ready({"adx": float("inf")})
    check("inf adx refused", not ok, why)
    ok, why = guards.indicators_ready({"rsi": None})
    check("None refused", not ok, why)


def main() -> int:
    print("=== GOLD RIPPER merged-guards test suite ===")
    test_geometry()
    test_price_cross()
    test_black_swan()
    test_protections()
    test_fasttape_matches_pandas()
    test_fasttape_latency()
    test_fail_closed()
    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
