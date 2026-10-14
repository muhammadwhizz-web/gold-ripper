"""
GOLD RIPPER :: Divergence self-check
====================================
The bot audits ITSELF: are the live journal fills still statistically
consistent with the walk-forward calibration the strategy was sized on?

Three honest tests (mirrors the dashboard's /api/ripper/divergence):
  1. win-rate binomial z-score   vs calibration OOS win rate
  2. expectancy z-score          (SE from live pnl dispersion)
  3. profit-factor ratio band    vs calibration OOS PF

Verdict ladder:
  WARMING_UP  < min trades (12) - nothing means anything yet
  ALIGNED     |z| < 1.5 and PF >= 0.6x calibration
  WATCH       |z| >= 1.5 - keep looking
  DRIFTED     |z| >= 2.5 or PF < 0.6x calibration - strategy is off-script

ripper.py calls check() on a timer, appends every verdict to a history
file (drift_history[_TAG].json, capped) so the dashboard can draw the
verdict TIMELINE, and pushes a Telegram alert when the verdict latches
to DRIFTED (cooldown-guarded so it never spams).
"""
from __future__ import annotations

import csv
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

MIN_TRADES = 12
ROLL_WINDOW = 20
WATCH_Z = 1.5
DRIFT_Z = 2.5
PF_FLOOR_RATIO = 0.6


def _read_pnls(journal_file: Path) -> list[float]:
    """Closed-trade PnLs, oldest first. Tolerates the extended schema."""
    try:
        with journal_file.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except Exception:  # noqa: BLE001 - missing/corrupt journal = no data
        return []
    out: list[float] = []
    for r in rows:
        try:
            out.append(float(r.get("pnl_usd") or 0.0))
        except (TypeError, ValueError):
            continue
    return out


def _expected(calibration_file: Path) -> dict | None:
    try:
        cal = json.loads(calibration_file.read_text())
        oos = cal.get("metrics_out_sample") or {}
        wr = oos.get("win_rate")
        pf = oos.get("profit_factor")
        if wr is None:
            return None
        exp = oos.get("net") / max(int(oos.get("trades") or 1), 1)
        return {"win_rate": float(wr) / 100.0,
                "profit_factor": float(pf) if pf is not None else None,
                "expectancy": float(exp)}
    except Exception:  # noqa: BLE001
        return None


def _stats(pnls: list[float]) -> dict:
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw = sum(wins)
    gl = abs(sum(losses))
    mean = sum(pnls) / n if n else 0.0
    var = (sum((p - mean) ** 2 for p in pnls) / n) if n else 0.0
    return {"n": n,
            "win_rate": len(wins) / n if n else 0.0,
            "net": gw - gl,
            "profit_factor": (gw / gl) if gl > 0 else None,
            "expectancy": mean,
            "sd": math.sqrt(var)}


def _tests(obs: dict, exp: dict) -> dict:
    n = max(obs["n"], 1)
    z_wr = 0.0
    if exp["win_rate"] is not None and 0.0 < exp["win_rate"] < 1.0:
        se = math.sqrt(exp["win_rate"] * (1 - exp["win_rate"]) / n)
        if se > 0:
            z_wr = (obs["win_rate"] - exp["win_rate"]) / se
    z_exp = 0.0
    if obs["sd"] > 0:
        z_exp = (obs["expectancy"] - exp["expectancy"]) / \
                (obs["sd"] / math.sqrt(n))
    pf_ratio = None
    if obs["profit_factor"] is not None and exp["profit_factor"]:
        pf_ratio = obs["profit_factor"] / exp["profit_factor"]
    return {"z_win_rate": round(z_wr, 2),
            "z_expectancy": round(z_exp, 2),
            "pf_ratio": round(pf_ratio, 2) if pf_ratio is not None else None}


def _verdict(tests: dict, obs: dict, exp: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    aw, ae = abs(tests["z_win_rate"]), abs(tests["z_expectancy"])
    pf_floor_hit = (tests["pf_ratio"] is not None
                    and tests["pf_ratio"] < PF_FLOOR_RATIO)
    if pf_floor_hit:
        reasons.append(f"PF {obs['profit_factor']:.2f} is only "
                       f"{tests['pf_ratio']:.2f}x the calibration PF "
                       f"{exp['profit_factor']:.2f}")
    if ae >= WATCH_Z:
        reasons.append(f"expectancy z={tests['z_expectancy']:+.2f} "
                       + ("(drift threshold)" if ae >= DRIFT_Z
                          else "(watch band)"))
    if aw >= WATCH_Z:
        reasons.append(f"win-rate z={tests['z_win_rate']:+.2f} "
                       + ("(drift threshold)" if aw >= DRIFT_Z
                          else "(watch band)"))
    if max(aw, ae) >= DRIFT_Z or pf_floor_hit:
        if not pf_floor_hit:
            reasons.append(f"z {max(aw, ae):.2f} >= {DRIFT_Z} "
                           f"drift threshold")
        return "DRIFTED", reasons
    if max(aw, ae) >= WATCH_Z:
        return "WATCH", reasons
    reasons.append("live fills within statistical noise of calibration")
    return "ALIGNED", reasons


def check(cfg) -> dict:
    """Full self-check against this instance's journal + calibration."""
    exp = _expected(cfg.calibration_file)
    if exp is None:
        return {"status": "NO_CALIBRATION",
                "reasons": [f"no calibration at {cfg.calibration_file.name} "
                            f"- run python train.py"
                            + (f" --tag {cfg.symbol_tag.lower()}"
                               if cfg.symbol_tag else "")],
                "n": 0}
    pnls = _read_pnls(cfg.journal_file)
    if len(pnls) < MIN_TRADES:
        return {"status": "WARMING_UP",
                "reasons": [f"{len(pnls)}/{MIN_TRADES} closed trades - "
                            f"verdicts mean nothing before that"],
                "n": len(pnls)}
    obs = _stats(pnls)
    tests = _tests(obs, exp)
    status, reasons = _verdict(tests, obs, exp)

    out = {"status": status, "n": obs["n"], "tests": tests,
           "observed": {"win_rate": round(obs["win_rate"] * 100, 1),
                        "profit_factor": obs["profit_factor"],
                        "expectancy": round(obs["expectancy"], 3),
                        "net": round(obs["net"], 2)},
           "expected": {"win_rate": round(exp["win_rate"] * 100, 1),
                        "profit_factor": exp["profit_factor"],
                        "expectancy": round(exp["expectancy"], 3)},
           "reasons": reasons}

    # fast-reaction rolling verdict over the last ROLL_WINDOW trades
    if len(pnls) > ROLL_WINDOW:
        rob = _stats(pnls[-ROLL_WINDOW:])
        rtests = _tests(rob, exp)
        rstatus, _r = _verdict(rtests, rob, exp)
        out["rolling"] = {"status": rstatus, "n": ROLL_WINDOW}
    return out


def record_history(cfg, rep: dict, cap: int = 336) -> None:
    """Append a self-check verdict to drift_history[_TAG].json (capped at
    ~3.5 days of 15-min checks). Corrupt file -> start fresh, never raise."""
    try:
        try:
            hist = json.loads(cfg.drift_history_file.read_text())
        except Exception:  # noqa: BLE001 - missing/corrupt = fresh
            hist = []
        if not isinstance(hist, list):
            hist = []
        status = rep.get("status")
        if not status or status == "NO_CALIBRATION":
            return
        hist.append({"ts": time.time(),
                     "iso": datetime.now(timezone.utc).isoformat(),
                     "status": status,
                     "n": rep.get("n", 0)})
        if len(hist) > cap:
            hist = hist[-cap:]
        tmp = cfg.drift_history_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(hist))
        tmp.replace(cfg.drift_history_file)
    except Exception:  # noqa: BLE001 - history must never kill the loop
        pass


def read_history(cfg, limit: int = 96) -> list[dict]:
    """The verdict timeline for the dashboard (oldest -> newest)."""
    try:
        hist = json.loads(cfg.drift_history_file.read_text())
        if isinstance(hist, list):
            return hist[-limit:]
    except Exception:  # noqa: BLE001
        pass
    return []


def alert_text(report: dict) -> str:
    """Telegram body for a DRIFTED report."""
    o = report.get("observed", {})
    e = report.get("expected", {})
    t = report.get("tests", {})
    lines = ["<b>DRIFT ALERT</b> - live fills no longer match calibration",
             f"trades {report.get('n', 0)} · WR {o.get('win_rate')}% vs "
             f"{e.get('win_rate')}% expected",
             f"PF {o.get('profit_factor')} vs {e.get('profit_factor')} "
             f"expected · expectancy ${o.get('expectancy')} vs "
             f"${e.get('expectancy')}",
             f"z(WR) {t.get('z_win_rate')} · z(exp) {t.get('z_expectancy')}"]
    for r in report.get("reasons", []):
        lines.append(f"· {r}")
    lines.append("rails still cap the damage - consider retraining "
                 "(python train.py) or pausing the bot.")
    return "\n".join(lines)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
