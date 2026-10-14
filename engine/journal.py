"""
GOLD RIPPER :: Journal
======================
Append-only CSV trade journal + JSONL event log. The ripper's black box.

Schema v2 (backward compatible): a fresh journal also carries
  hold_s    - seconds the position was open
  slip_bps  - exit-fill slippage/spread stress paid (paper stress model)
  spike     - exit fired during a spread spike (true/false)
Older 14-column journals keep working everywhere (readers use the first
14 columns; new ones are only appended when the header has them).
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

BASE_COLS = ["ts_open_utc", "ts_close_utc", "mode", "symbol", "side",
             "module", "size", "entry", "exit", "sl", "tp1",
             "reason", "pnl_usd", "balance_after"]
EXT_COLS = ["hold_s", "slip_bps", "spike"]


def _has_ext_cols(f: Path) -> bool:
    """True when the existing journal header carries the v2 columns."""
    try:
        with f.open("r", encoding="utf-8") as fh:
            return "hold_s" in fh.readline()
    except Exception:  # noqa: BLE001 - missing file handled by caller
        return False


def journal_trade(cfg, row: dict) -> None:
    f: Path = cfg.journal_file
    f.parent.mkdir(parents=True, exist_ok=True)
    new = not f.exists()
    ext = new or _has_ext_cols(f)
    with f.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(BASE_COLS + EXT_COLS)
        vals = [row.get("ts_open"), row.get("ts_close"), row.get("mode"),
                row.get("symbol"), row.get("side"), row.get("module"),
                row.get("size"), row.get("entry"), row.get("exit"),
                row.get("sl"), row.get("tp1"), row.get("reason"),
                row.get("pnl"), row.get("balance")]
        if ext:
            vals += [row.get("hold_s", ""), row.get("slip_bps", ""),
                     row.get("spike", "")]
        w.writerow(vals)


def log_event(cfg, kind: str, data: dict) -> None:
    f: Path = cfg.state_file.parent / "ripper_events.jsonl"
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": kind, **data}) + "\n")
    except OSError:
        pass
