"""
GOLD RIPPER :: Watchdog
=======================
Checks data/heartbeat.json; if the ripper is stale (dead, wedged, frozen),
restarts it. Runs as its own process via systemd/Task Scheduler.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HEARTBEAT = ROOT / "data" / "heartbeat.json"
STALE_SECONDS = 300


def main() -> int:
    print("[watchdog] guarding the ripper")
    while True:
        time.sleep(60)
        try:
            if not HEARTBEAT.exists():
                _restart("no heartbeat file")
                continue
            import json
            data = json.loads(HEARTBEAT.read_text())
            age = time.time() - float(data.get("ts", 0))
            if age > STALE_SECONDS:
                _restart(f"heartbeat stale {age:.0f}s")
        except Exception as e:  # noqa: BLE001
            print(f"[watchdog] check error: {e}")


def _restart(why: str) -> None:
    print(f"[watchdog] ripper dead ({why}) - restarting")
    try:
        subprocess.Popen(
            [sys.executable, str(ROOT / "ripper.py")],
            cwd=str(ROOT),
            stdout=open(ROOT / "data" / "ripper_stdout.log", "ab"),
            stderr=subprocess.STDOUT)
    except Exception as e:  # noqa: BLE001
        print(f"[watchdog] restart failed: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
