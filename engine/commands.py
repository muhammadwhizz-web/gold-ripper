"""
GOLD RIPPER :: Command channel
==============================
The dashboard (or any operator tool) talks to the bot through a command
FILE, not a socket - so the bot needs zero inbound ports and works
identically on a laptop, a VPS or behind systemd.

Protocol (write-then-ack, crash safe):
  1. operator writes   data/commands[_TAG].json   {"action", "id", "ts"}
  2. bot polls it every loop, EXECUTES, then atomically:
       - writes data/commands_status[_TAG].json  {"action","id","ok","ts","detail"}
       - deletes the command file  ->  a command can never run twice
  3. the dashboard reads the status file (or the heartbeat "cmd" field)
     to show EXECUTED / QUEUED states

Actions:
  pause           stop taking NEW entries (exits + rails stay live).
                  STICKY: writes pause.flag so the pause survives a restart -
                  a crashed-but-paused bot must never wake up hunting.
  resume          clear the pause (removes pause.flag)
  close_all       market-close every open position + cancel the pending
                  limit (the "kill switch")
  cancel_pending  cancel the armed maker limit only
  retrain         re-run the calibration INSIDE the bot's own interpreter
                  and venv (fetch_history -> train.py); progress rides the
                  heartbeat "train" field so the dashboard can watch it
  feed_check      force a fresh tape pull (Bitget venue OHLCV -> Yahoo ->
                  CSV) and ack WHICH layer answered, how many bars, how
                  fast - the "is my data alive?" button
  reconcile_now   force a venue reconciliation cycle right now (LIVE
                  only) instead of waiting for RIPPER_RECONCILE_MIN -
                  the fresh report rides the heartbeat within one loop

A stale command file (> 10 min old with no matching id in the status
file) is treated as dead and dropped - never executes twice, never
executes something the operator forgot about weeks ago.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

VALID_ACTIONS = ("pause", "resume", "close_all", "cancel_pending",
                 "retrain", "feed_check", "reconcile_now")
STALE_S = 600  # a queued command older than 10 minutes never executes


def commands_file(cfg) -> Path:
    return cfg.commands_file


def status_file(cfg) -> Path:
    return cfg.commands_status_file


def read_pending(cfg) -> dict | None:
    """The currently queued command (validated, freshness-checked)."""
    try:
        cmd = json.loads(cfg.commands_file.read_text())
    except Exception:  # noqa: BLE001 - missing/garbled file = nothing to do
        return None
    if not isinstance(cmd, dict):
        return None
    action = str(cmd.get("action", "")).lower().strip()
    if action not in VALID_ACTIONS:
        return None
    ts = cmd.get("ts")
    if isinstance(ts, (int, float)) and time.time() - float(ts) > STALE_S:
        # operator command went stale unexecuted (bot was offline) - drop it
        drop(cfg, cmd)
        write_status(cfg, {"action": action, "id": cmd.get("id"), "ok": False,
                           "ts": time.time(),
                           "detail": "expired unexecuted (>10 min old)"})
        return None
    return cmd


def write(cfg, action: str) -> dict:
    """Operator side: queue a command (the dashboard API uses this)."""
    if action not in VALID_ACTIONS:
        raise ValueError(f"unknown action {action}")
    cmd = {"action": action, "id": f"{int(time.time()*1000):x}",
           "ts": time.time(),
           "iso": datetime_iso()}
    cfg.commands_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.commands_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(cmd))
    tmp.replace(cfg.commands_file)
    return cmd


def read_status(cfg) -> dict | None:
    try:
        return json.loads(cfg.commands_status_file.read_text())
    except Exception:  # noqa: BLE001
        return None


def write_status(cfg, payload: dict) -> None:
    try:
        cfg.commands_status_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.commands_status_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str))
        tmp.replace(cfg.commands_status_file)
    except Exception:  # noqa: BLE001 - status write must never kill the loop
        pass


def drop(cfg, cmd: dict) -> None:
    """Remove the command file after execution (or staleness)."""
    try:
        cfg.commands_file.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    _ = cmd  # kept for log context at call sites


def datetime_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- sticky pause
def set_pause_flag(cfg, note: str = "") -> None:
    """Write pause.flag: the pause now survives any restart/crash."""
    try:
        cfg.pause_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.pause_file.write_text(json.dumps(
            {"iso": datetime_iso(), "note": note}))
    except Exception:  # noqa: BLE001 - flag write must never kill the loop
        pass


def clear_pause_flag(cfg) -> None:
    try:
        cfg.pause_file.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def pause_flag_active(cfg) -> bool:
    try:
        return cfg.pause_file.exists()
    except Exception:  # noqa: BLE001
        return False
