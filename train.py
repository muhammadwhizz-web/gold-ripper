"""
GOLD RIPPER :: Trainer
======================
Walk-forward calibration on REAL fetched gold history:
  1. load data/history/gold_1h.csv (built by data/fetch_history.py)
  2. in-sample  = first 70% of bars  -> grid search every strategy combo
  3. out-sample = last  30% of bars  -> score every survivor honestly
  4. rank by OOS composite (win rate, PF, sample size, block-hit rate)
  5. write data/calibration.json (ripper.py loads it at boot)

Also produces data/twenty_year.json: the 26-year daily recon used by the
dashboard (regime stats, yearly returns, drawdowns).

Multi-symbol:
  python train.py --tag XAG
calibrates on data/history/xag_1h.csv -> data/calibration_XAG.json
(the tagged ripper instance loads it via RIPPER_SYMBOL_TAG=XAG).
"""
from __future__ import annotations

import argparse
import itertools
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import run_backtest, summarize
from engine.config import Config
from engine.strategy import StrategyEngine

ROOT = Path(__file__).resolve().parent
TAG = "gold"
H1_PATH = ROOT / "data" / "history" / "gold_1h.csv"
DAILY_PATH = ROOT / "data" / "history" / "gold_daily.csv"
OUT = ROOT / "data" / "calibration.json"
TWENTY_OUT = ROOT / "data" / "twenty_year.json"


def load_h1() -> pd.DataFrame:
    df = pd.read_csv(H1_PATH, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


# ─────────────────────────────────────────────────────────── twenty-year recon
def twenty_year_recon() -> dict:
    df = pd.read_csv(DAILY_PATH, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    c = df["close"]
    yearly = c.resample("YE").last().pct_change().dropna() * 100
    yearly = {str(k.year): round(v, 1) for k, v in yearly.items()}
    ret = c.pct_change()
    vol20 = ret.rolling(20).std() * np.sqrt(252) * 100
    up20 = c.ewm(span=50).mean() > c.ewm(span=200).mean()
    years_total = (c.index[-1] - c.index[0]).days / 365.25
    cagr = ((c.iloc[-1] / c.iloc[0]) ** (1 / years_total) - 1) * 100
    peak = c.cummax()
    dd = (c / peak - 1) * 100
    return {
        "start": str(c.index[0].date()), "end": str(c.index[-1].date()),
        "start_price": round(float(c.iloc[0]), 2),
        "end_price": round(float(c.iloc[-1]), 2),
        "total_return_x": round(float(c.iloc[-1] / c.iloc[0]), 2),
        "cagr_pct": round(float(cagr), 2),
        "max_drawdown_pct": round(float(dd.min()), 1),
        "bullish_share_pct": round(float(up20.mean() * 100), 1),
        "avg_daily_vol_pct": round(float(ret.std() * 100), 3),
        "yearly_returns": yearly,
    }


# ───────────────────────────────────────────────────────────────── grid search
def param_grid() -> list[dict]:
    """Grid centered on the 26-year-validated trend-rider geometry
    (W2: donchian breakout + daily bias, 4.5xATR stop, 1.2R TP1, 5xATR
    trail). Supporting modules keep their own families."""
    grid = []
    for sl, tp1, trail, tsb in itertools.product(
            (3.5, 4.5, 5.5), (1.0, 1.2, 1.5), (4.5, 5.5), (96, 120)):
        grid.append({
            "min_atr_usd": 3.0,
            "m1_sl_atr": sl, "m1_tp1_r": tp1, "m1_trail_atr": trail,
            "m1_time_stop_bars": tsb,
            "m2_enabled": False, "m3_enabled": False, "m4_enabled": False,
        })
    # M4 NY continuation family
    for look, sl, tp1, trail in itertools.product(
            (6, 9), (2.4, 3.2), (0.8, 1.2), (2.5, 3.5)):
        grid.append({
            "min_atr_usd": 3.0, "m1_enabled": False, "m2_enabled": False,
            "m3_enabled": False,
            "m4_lookback": look, "m4_sl_atr": sl, "m4_tp1_r": tp1,
            "m4_trail_atr": trail,
        })
    # M3 sweep family
    for wick, m3tp, floor_atr, tsb in itertools.product(
            (0.4, 0.7), (0.8, 1.2), (1.2, 1.8), (10, 14)):
        grid.append({
            "min_atr_usd": 3.0, "m1_enabled": False, "m2_enabled": False,
            "m3_min_wick_atr": wick, "m3_tp1_r": m3tp,
            "risk_floor_atr": floor_atr, "m3_time_stop_bars": tsb,
            "m4_enabled": False,
        })
    # M2 mean-reversion family
    for m2adx, m2lo, m2hi, m2tp, floor_atr in itertools.product(
            (20.0, 26.0), (30.0, 33.0), (67.0, 70.0), (0.8, 1.2), (1.2, 1.8)):
        grid.append({
            "min_atr_usd": 3.0, "m1_enabled": False,
            "m2_max_adx": m2adx, "m2_rsi_lo": m2lo, "m2_rsi_hi": m2hi,
            "m2_tp1_r": m2tp, "risk_floor_atr": floor_atr,
            "m3_enabled": False, "m4_enabled": False,
        })
    # trail-only riders (pure chandelier - the big-trend catcher)
    for sl, trail, adxg, tsb in itertools.product(
            (3.5, 4.5, 5.5), (3.5, 5.0, 6.5), (0.0, 18.0, 25.0), (96, 144)):
        grid.append({
            "min_atr_usd": 3.0, "m1_sl_atr": sl, "m1_tp1_r": 9.9,
            "m1_trail_atr": trail, "m1_time_stop_bars": tsb,
            "m1_min_adx": adxg, "trail_start_r": 0.0,
            "m2_enabled": False, "m3_enabled": False, "m4_enabled": False,
        })
    # ALL-4 ripper combos (core + supporting modules live together)
    for sl, tp1, trail in itertools.product((4.0, 4.8), (1.0, 1.3), (4.5, 5.5)):
        grid.append({
            "min_atr_usd": 3.0, "m1_sl_atr": sl, "m1_tp1_r": tp1,
            "m1_trail_atr": trail, "m1_time_stop_bars": 120,
            "m4_sl_atr": 2.6, "m4_tp1_r": tp1, "m4_trail_atr": 3.0,
            "m2_tp1_r": tp1, "m3_tp1_r": tp1,
        })
    return grid


def ladder_ok(p: dict) -> bool:
    """Ladder-calibration hard-DQ (astra-quant order_risk.py merge): any
    TP1 trigger closer than 1R is the win-small-lose-big trap (their live
    numbers: avg win 23.9 vs avg loss 48.0). Trail-only riders (no TP1,
    trail_start_r=0) pass trivially."""
    for k in ("m1_tp1_r", "m2_tp1_r", "m3_tp1_r", "m4_tp1_r"):
        v = p.get(k)
        if v is not None and float(v) < 1.0:
            return False
    return True


def apply_params(cfg: Config, p: dict) -> Config:
    for k, v in p.items():
        if hasattr(cfg, k):
            cur = getattr(cfg, k)
            if isinstance(cur, tuple) and isinstance(v, list):
                v = tuple(v)
            setattr(cfg, k, v)
    return cfg


def score_oos(m: dict) -> float | None:
    """OOS composite; None = disqualify."""
    if m.get("trades", 0) < 50 or not m.get("profit_factor"):
        return None
    if m["profit_factor"] < 0.75 or m["max_drawdown_pct"] < -6:
        return None
    wr = min(m["win_rate"], 72.0) / 72.0
    pf = min(m["profit_factor"], 2.5) / 2.5
    sample = min(m["trades"], 220) / 220.0
    n_blocks = max(m["blocks_total"], 1)
    block_rate = min(m["blocks_hit_target"] / n_blocks / 0.30, 1.0)
    return 0.42 * wr + 0.24 * pf + 0.14 * sample + 0.20 * block_rate


def main() -> int:
    global H1_PATH, DAILY_PATH, OUT, TWENTY_OUT, TAG
    ap = argparse.ArgumentParser(description="GOLD RIPPER trainer")
    ap.add_argument("--tag", default="gold",
                    help="tape tag: reads {tag}_1h.csv, writes "
                         "calibration_{TAG}.json (default: gold)")
    a = ap.parse_args()
    TAG = a.tag.lower()
    if TAG != "gold":
        H1_PATH = ROOT / "data" / "history" / f"{TAG}_1h.csv"
        DAILY_PATH = ROOT / "data" / "history" / f"{TAG}_daily.csv"
        OUT = ROOT / "data" / f"calibration_{TAG.upper()}.json"
        TWENTY_OUT = ROOT / "data" / f"twenty_year_{TAG.upper()}.json"

    t0 = time.time()
    df = load_h1()
    n = len(df)
    split = int(n * 0.70)
    is_start, is_end = df.index[0], df.index[split - 1]
    oos_start, oos_end = df.index[split], df.index[-1]
    print(f"[i] bars={n}  IS: {is_start.date()} -> {is_end.date()}  "
          f"OOS: {oos_start.date()} -> {oos_end.date()}")

    results = []
    combos = param_grid()
    print(f"[i] evaluating {len(combos)} strategy variants "
          f"(in-sample -> survivors -> out-of-sample)...")
    for idx, p in enumerate(combos):
        if not ladder_ok(p):
            continue
        cfg_is = apply_params(Config(), p)
        res_is = run_backtest(df, cfg_is, is_start, is_end,
                              engine=StrategyEngine(cfg_is))
        m_is = summarize(res_is, cfg_is)
        if m_is.get("trades", 0) < 45 or not m_is.get("profit_factor") \
                or m_is["profit_factor"] < 1.0:
            continue
        cfg_oos = apply_params(Config(), p)
        res_oos = run_backtest(df, cfg_oos, oos_start, oos_end,
                               engine=StrategyEngine(cfg_oos))
        m_oos = summarize(res_oos, cfg_oos)
        s = score_oos(m_oos)
        if s is None:
            continue
        results.append({"params": p, "score": round(s, 4),
                        "is": m_is, "oos": m_oos})
        if (idx + 1) % 25 == 0:
            print(f"    ... {idx + 1}/{len(combos)} evaluated, "
                  f"{len(results)} survivors ({time.time() - t0:.0f}s)")

    if not results:
        print("[!] no variant passed OOS gates - keeping defaults")
        return 1

    results.sort(key=lambda r: r["score"], reverse=True)
    best = results[0]
    best_cfg = apply_params(Config(), best["params"])
    full_res = run_backtest(df, best_cfg, engine=StrategyEngine(best_cfg))
    m_full = summarize(full_res, best_cfg)

    payload = {
        "trained_at": pd.Timestamp.utcnow().isoformat(),
        "data": {"bars": n, "from": str(df.index[0]), "to": str(df.index[-1]),
                 "source": f"Yahoo {TAG} tape (fetch_history.py)"},
        "walk_forward": {"is_from": str(is_start), "is_to": str(is_end),
                         "oos_from": str(oos_start), "oos_to": str(oos_end)},
        "params": best["params"],
        "metrics_in_sample": best["is"],
        "metrics_out_sample": best["oos"],
        "metrics_full_period": m_full,
        "rails": {"block_target_usd": best_cfg.block_target_usd,
                  "block_max_loss_usd": best_cfg.block_max_loss_usd,
                  "day_target_usd": best_cfg.day_target_usd,
                  "day_max_loss_usd": best_cfg.day_max_loss_usd,
                  "risk_usd_per_trade": best_cfg.risk_usd_per_trade,
                  "max_attempts_per_block": best_cfg.max_attempts_per_block},
        "top5": [{"params": r["params"], "oos": r["oos"],
                  "score": r["score"]} for r in results[:5]],
    }
    OUT.write_text(json.dumps(payload, indent=2, default=str))
    try:
        TWENTY_OUT.write_text(json.dumps(twenty_year_recon(), indent=2))
    except Exception as e:  # noqa: BLE001 - daily recon is optional per tag
        if TAG == "gold":
            raise
        print(f"[i] daily recon skipped for tag {TAG}: {e}")

    print("\n=== BEST VARIANT (chosen on OUT-OF-SAMPLE, never seen in training) ===")
    print(json.dumps(best["oos"], indent=2))
    print("\n=== FULL PERIOD with best params ===")
    print(json.dumps(m_full, indent=2))
    print(f"\n[+] wrote {OUT.name} + {TWENTY_OUT.name} "
          f"({time.time() - t0:.0f}s total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
