#!/usr/bin/env python3
"""Round-9 smoke test: engine/reconcile.py OK + GAP paths with a fake broker.

Run:  python scripts/test_reconcile.py
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RIPPER_SYMBOL_TAG"] = "RT9"

from engine.config import Config  # noqa: E402
from engine import reconcile  # noqa: E402

NOW = time.time()
ISO = lambda e: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(e))

cfg = Config(symbol_tag="RT9")
cfg.mode = "LIVE"

# synthetic LIVE journal: one LONG opened 20 min ago
cfg.journal_file.parent.mkdir(parents=True, exist_ok=True)
cfg.journal_file.write_text(
    "ts_open,ts_close,mode,symbol,side,module,size,entry,exit,sl,tp1,"
    "reason,pnl,balance,hold_s,slip_bps,spike\n"
    f"{ISO(NOW - 1200)},{ISO(NOW - 600)},LIVE,XAU,LONG,M1_TREND,0.05,"
    "4000.0,4002.0,3990.0,4006.0,test,1.5,1001.5,600,0.4,0\n")

state = cfg.reconcile_state_file
if state.exists():
    state.unlink()

FILLS_MATCH = [
    {"ts": NOW - 1200, "iso": ISO(NOW - 1200), "side": "BUY", "px": 4000.1,
     "amount": 0.05, "fee": -0.24, "reduce_only": False, "oid": "o1"},
    {"ts": NOW - 600, "iso": ISO(NOW - 600), "side": "SELL", "px": 4002.0,
     "amount": 0.05, "fee": -0.24, "reduce_only": True, "oid": "o1"},
]


class FakeOK:
    @staticmethod
    def recent_fills(since_iso=None):
        return list(FILLS_MATCH)


class FakeGap:
    @staticmethod
    def recent_fills(since_iso=None):
        # same fills + one SELL entry fill the journal NEVER recorded
        return FILLS_MATCH + [
            {"ts": NOW - 300, "iso": ISO(NOW - 300), "side": "SELL",
             "px": 4001.0, "amount": 0.03, "fee": -0.1,
             "reduce_only": False, "oid": "ghost"}]


class FakeDown:
    @staticmethod
    def recent_fills(since_iso=None):
        raise RuntimeError("connect ETIMEDOUT")


def main() -> int:
    fails = []
    # 1. paper mode -> SKIP
    cfg.mode = "PAPER"
    rep = reconcile.run(cfg, FakeOK(), force=True)
    if rep["status"] != "SKIP":
        fails.append(f"paper expected SKIP got {rep['status']}")
    cfg.mode = "LIVE"

    # 2. all venue fills matched -> OK (exit counted separately)
    if state.exists():
        state.unlink()
    rep = reconcile.run(cfg, FakeOK(), force=True)
    ok = rep["status"] == "OK" and rep["matched_n"] == 1 \
        and rep["entry_n"] == 1 and rep["exit_n"] == 1
    if not ok:
        fails.append(f"OK path: {rep}")

    # 2b. fee-weighted venue-exit PnL estimate: LONG 0.05 @ 4000 closed by
    # SELL 0.05 @ 4002 -> (4002-4000)*0.05 + fee(-0.24) = 0.10 - 0.24 = -0.14
    ep = rep.get("exit_pnl_est")
    if ep is None or abs(ep - (-0.14)) > 0.005:
        fails.append(f"exit pnl est: expected -0.14 got {ep}")
    if rep.get("fees_est") != 0.48:
        fails.append(f"fees est: expected 0.48 got {rep.get('fees_est')}")
    if "est $-0.14" not in rep.get("note", ""):
        fails.append(f"OK note missing est pnl: {rep.get('note')}")

    # 3. ghost venue fill -> GAP, unmatched carries the details
    if state.exists():
        state.unlink()
    rep = reconcile.run(cfg, FakeGap(), force=True)
    if rep["status"] != "GAP" or rep.get("unmatched_n") != 1:
        fails.append(f"GAP path: {rep}")
    elif rep["unmatched"][0]["side"] != "SELL":
        fails.append(f"GAP detail side: {rep['unmatched']}")

    # 4. venue down -> ERROR with the reason
    if state.exists():
        state.unlink()
    rep = reconcile.run(cfg, FakeDown(), force=True)
    if rep["status"] != "ERROR" or "ETIMEDOUT" not in rep.get("note", ""):
        fails.append(f"ERROR path: {rep}")

    # 5. interval gate: a second non-forced call right after returns cache
    if state.exists():
        state.unlink()
    rep1 = reconcile.run(cfg, FakeGap(), force=True)
    rep2 = reconcile.run(cfg, FakeOK())  # interval-gated -> cached GAP
    if rep2.get("status") != "GAP":
        fails.append(f"cache path expected cached GAP got {rep2.get('status')}")
    _ = rep1

    # 6. telegram alert text renders
    txt = reconcile.alert_text(rep1)
    if "RECONCILE GAP" not in txt or "ghost" not in str(rep1):
        fails.append(f"alert text: {txt[:120]}")

    # 7. tagging: state file must be tag-isolated
    if state.name != "reconcile_RT9.json":
        fails.append(f"state file not tagged: {state.name}")

    # 8. no pricing basis -> exit_pnl_est refuses to guess (None, not 0)
    if state.exists():
        state.unlink()
    cfg.journal_file.write_text(
        "ts_open,ts_close,mode,symbol,side,module,size,entry,exit,sl,tp1,"
        "reason,pnl,balance,hold_s,slip_bps,spike\n"
        f"{ISO(NOW - 7200)},{ISO(NOW - 7000)},LIVE,XAU,SHORT,M1_TREND,0.05,"
        "4000.0,3998.0,4010.0,3994.0,test,-1.5,998.5,200,0.4,0\n")
    orphan_fills = [
        {"ts": NOW - 1200, "iso": ISO(NOW - 1200), "side": "BUY",
         "px": 4000.1, "amount": 0.05, "fee": -0.24,
         "reduce_only": True, "oid": "algo-leg"},
    ]

    class FakeOrphan:
        @staticmethod
        def recent_fills(since_iso=None):
            return list(orphan_fills)

    rep = reconcile.run(cfg, FakeOrphan(), force=True)
    # the BUY exit implies a SHORT close, but exit pricing requires a venue
    # ENTRY matched in this fills window - there are none (the position was
    # opened before the cursor), so the estimate refuses to guess (None)
    if rep.get("exit_pnl_est") is not None:
        fails.append(f"orphan exit should refuse to price: {rep.get('exit_pnl_est')}")

    # cleanup
    if state.exists():
        state.unlink()
    cfg.journal_file.unlink(missing_ok=True)

    if fails:
        print("FAIL")
        for f in fails:
            print(" -", f)
        return 1
    print("PASS: reconcile OK/GAP/ERROR/cache/tag paths all verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
