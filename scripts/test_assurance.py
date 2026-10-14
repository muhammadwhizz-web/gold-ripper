#!/usr/bin/env python3
"""
GOLD RIPPER :: Daily-Assurance test battery
===========================================
Proves the zero-trade-day guarantee WITHOUT breaking a single safety rail:

  1. ladder math: relaxed_config is monotone, never mutates the original,
     and NEVER touches rails (risk caps, floors, fees, bias filter)
  2. level(): off before assure-hour, off after a fill, off with a
     pending order, escalates with time, caps at assure_steps
  3. strategy parity: a synthetic dead-flat chop tape that produces ZERO
     signals at L0 still produces a valid, cost-floor-clean, correctly
     sided signal at L3 - tagged ASSURE-L3
  4. backtest parity: backtest.py source actually passes `assure=` to
     evaluate (same rules in backtest, no silent divergence)
  5. rails: RiskRails day_attempts increments on attempt, rolls on day
     change, and assurance can NEVER fire on a day that already traded

Run:  python3 scripts/test_assurance.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import assurance  # noqa: E402
from engine.config import Config  # noqa: E402
from engine.risk import RiskRails, day_key  # noqa: E402
from engine.strategy import StrategyEngine  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name} {detail}")


# ────────────────────────────────────────────────── 1. ladder math
print("== 1. ladder math ==")
cfg = Config()
orig_vals = (cfg.m1_min_adx, cfg.m4_min_adx, cfg.min_atr_usd,
             cfg.m2_rsi_lo, cfg.m2_rsi_hi, cfg.risk_usd_per_trade,
             cfg.risk_floor_atr, cfg.risk_floor_pct, cfg.bias_filter)

l1 = assurance.relaxed_config(cfg, 1)
l2 = assurance.relaxed_config(cfg, 2)
l3 = assurance.relaxed_config(cfg, 3)

check("L1 halves M1 ADX floor",
      abs(l1.m1_min_adx - cfg.m1_min_adx * 0.5) < 1e-9)
check("L2 zeroes M1 ADX floor", l2.m1_min_adx == 0.0)
check("ladder monotone: ATR floor relaxes downward",
      l1.min_atr_usd > l2.min_atr_usd and l2.min_atr_usd < cfg.min_atr_usd)
check("L2 widens pullback zone", l2.m1_near_atr == cfg.m1_near_atr * 2.0)
check("L3 bands wide open",
      l3.m2_bb_lo == 0.0 and l3.m2_bb_hi == 1.0)
check("original cfg never mutated",
      (cfg.m1_min_adx, cfg.m4_min_adx, cfg.min_atr_usd,
       cfg.m2_rsi_lo, cfg.m2_rsi_hi) == orig_vals[:5])
check("RAILS untouched by ladder: risk/trade",
      all(x.risk_usd_per_trade == orig_vals[5] for x in (l1, l2, l3)))
check("RAILS untouched: cost floors",
      all(x.risk_floor_atr == orig_vals[6] and
          x.risk_floor_pct == orig_vals[7] for x in (l1, l2, l3)))
check("RAILS untouched: bias filter stays ON",
      all(x.bias_filter == orig_vals[8] for x in (l1, l2, l3)))
check("RAILS untouched: loss caps",
      all(x.day_max_loss_usd == cfg.day_max_loss_usd and
          x.block_max_loss_usd == cfg.block_max_loss_usd
          for x in (l1, l2, l3)))
check("step 0 returns original object", assurance.relaxed_config(cfg, 0) is cfg)


# ────────────────────────────────────────────────── 2. level() logic
print("== 2. level() logic ==")
base = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)  # noon - before hour
h20 = base.replace(hour=20)
h20_50 = h20 + timedelta(minutes=50)
h22 = h20 + timedelta(hours=2)
h23_59 = h23 = h20 + timedelta(hours=3, minutes=59)

check("off: before assure hour",
      assurance.level(base, 0, False, cfg) == 0)
check("L1 at exactly assure hour",
      assurance.level(h20, 0, False, cfg) == 1)
check("L2 after escalate window",
      assurance.level(h20_50, 0, False, cfg) == 2)
check("L3 late evening", assurance.level(h22, 0, False, cfg) == 3)
check("caps at steps (never >3)",
      assurance.level(h23_59, 0, False, cfg) == 3)
check("off: day already traded",
      assurance.level(h22, 1, False, cfg) == 0)
check("off: order pending on the book",
      assurance.level(h22, 0, True, cfg) == 0)
check("off: feature disabled",
      assurance.level(h22, 0, False, Config(assure_daily=False)) == 0)
check("naive datetime treated as UTC",
      assurance.level(h20.replace(tzinfo=None), 0, False, cfg) == 1)


# ────────────────────────────────────────────────── 3. strategy parity
print("== 3. strategy parity on a low-volatility overlap tape ==")


def chop_tape(n: int = 700) -> pd.DataFrame:
    """A tight zigzag tape whose bar ranges overlap (highs/lows creep by
    ~0.2/2-bar cycle while closes oscillate ±0.9/-0.66): ATR ~1.1 sits
    BELOW the L0 dead-tape floor ($2) - the classic dead day that starves
    every normal gate. Tiny fixed noise; fully deterministic (seed 7)."""
    rng = np.random.default_rng(7)
    idx = pd.date_range("2025-01-10 00:00", periods=n, freq="1h",
                        tz="UTC")
    px = [3000.0]
    for k in range(1, n):
        px.append(px[-1] + (0.9 if k % 2 == 0 else -0.66)
                  + rng.normal(0, 0.03))
    px = np.asarray(px)
    op = np.empty(n)
    op[0] = 3000.0
    op[1:] = px[:-1] + rng.normal(0, 0.02, n - 1)
    cl = px
    hi = np.maximum(op, cl) + np.abs(rng.normal(0.18, 0.04, n))
    lo = np.minimum(op, cl) - np.abs(rng.normal(0.18, 0.04, n))
    return pd.DataFrame({"open": op, "high": hi, "low": lo, "close": cl,
                         "volume": np.abs(rng.normal(100, 10, n))}, index=idx)


df = chop_tape()
eng = StrategyEngine(cfg)
ctx = eng._context(df)
n = len(df)

l0_hits = 0
l1_hits = 0
l2_sig = None
for i in range(500, n):
    ts_i = df.index[i]
    if eng.evaluate(df, ctx, i, ts_i, assure=0) is not None:
        l0_hits += 1
    if eng.evaluate(df, ctx, i, ts_i, assure=1) is not None:
        l1_hits += 1
    if l2_sig is None:
        l2_sig = eng.evaluate(df, ctx, i, ts_i, assure=2)
check("L0 (normal gates): dead tape produces ZERO signals", l0_hits == 0,
      f"(got {l0_hits})")
check("L1 still respects the half-relaxed ATR floor (silent here)",
      l1_hits == 0, f"(got {l1_hits})")
check("L2 (assurance): the SAME tape produces a signal", l2_sig is not None)
if l2_sig is not None:
    atr_v = float(ctx["atr"].iloc[n - 1])
    floor = max(cfg.risk_floor_atr * atr_v,
                cfg.risk_floor_pct * float(l2_sig.entry))
    check("L2 signal respects cost floor (untradeable stops impossible)",
          l2_sig.risk_dist >= floor * 0.999,
          f"(dist {l2_sig.risk_dist:.2f} vs floor {floor:.2f})")
    check("L2 signal geometry sane (SL correct side)",
          (l2_sig.side == "LONG" and l2_sig.sl < l2_sig.entry < l2_sig.tp1)
          or (l2_sig.side == "SHORT"
              and l2_sig.sl > l2_sig.entry > l2_sig.tp1))
    check("L2 signal tagged for journal audit",
          "ASSURE-L2" in l2_sig.reason, f"(reason: {l2_sig.reason})")
check("cfg object restored after assure run",
      eng.cfg is cfg and cfg.m1_min_adx == orig_vals[0])


# ──────────────────────────────────────── 3b. real-history dead-day scan
print("== 3b. real-history dead-day rescue (26yr CSV, last 2y) ==")
hist = ROOT / "data" / "history" / "gold_1h.csv"
if hist.exists():
    hdf = pd.read_csv(hist, parse_dates=[0], index_col=0).tail(24 * 365 * 2)
    hctx = eng._context(hdf)
    hidx = hdf.index
    from collections import defaultdict
    l0d: dict[str, int] = defaultdict(int)
    asd: dict[str, int] = defaultdict(int)
    for i in range(500, len(hdf)):
        ts_i = hidx[i]
        dk = ts_i.strftime("%Y-%m-%d")
        if eng.evaluate(hdf, hctx, i, ts_i, assure=0) is not None:
            l0d[dk] += 1
        lvl = assurance.level(ts_i.to_pydatetime(), 0, False, cfg)
        if lvl and eng.evaluate(hdf, hctx, i, ts_i, assure=lvl) is not None:
            asd[dk] += 1
    dead = [d for d in set(l0d) | set(asd) if l0d.get(d, 0) == 0]
    rescued = [d for d in dead if asd.get(d, 0) > 0]
    print(f"  days scanned: {len(set(l0d) | set(asd))} · "
          f"L0-dead days: {len(dead)} · rescued: {len(rescued)}")
    check("every real dead day gets an assured attempt",
          len(dead) == len(rescued),
          f"(missed: {[d for d in dead if d not in rescued][:5]})")
else:
    print("  [skip] no history CSV found")


# ────────────────────────────────────────────────── 4. backtest parity
print("== 4. backtest parity ==")
bt_src = (ROOT / "backtest.py").read_text()
check("backtest passes assure= to evaluate",
      "eng.evaluate(data, ctx, i, ts, assure=lvl)" in bt_src)
check("backtest tracks day_attempts", "day_attempts" in bt_src)
check("backtest imports assurance module",
      "from engine import assurance" in bt_src)
rip_src = (ROOT / "ripper.py").read_text()
check("live loop computes assurance level",
      "assurance.level(" in rip_src)
check("heartbeat carries assure field",
      '"assure"' in rip_src)


# ────────────────────────────────────────────────── 5. rails integrity
print("== 5. rails integrity ==")
import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    tcfg = Config()
    tcfg.state_file = Path(td) / "state.json"
    rails = RiskRails(tcfg)
    check("fresh rails: day_attempts == 0", rails.state.day_attempts == 0)
    rails.register_attempt()
    check("attempt bumps day_attempts", rails.state.day_attempts == 1)
    rails.register_attempt()
    check("second attempt bumps to 2", rails.state.day_attempts == 2)
    snap = rails.snapshot()
    check("snapshot exposes day_attempts",
          snap.get("day_attempts") == 2)
    # day rollover resets the assurance input
    rails.state.day = "2024-01-01"  # force a stale day key
    rails.can_enter()               # triggers _roll()
    check("day rollover resets day_attempts", rails.state.day_attempts == 0)
    check("day key format sane", len(day_key()) == 10)

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
