"""
GOLD RIPPER :: Notifier
=======================
Telegram push alerts (optional): entries, exits, rail hits, errors.
Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env to arm.
All sends are fire-and-forget - a notification failure can NEVER
interrupt the trading loop.

CLI test:
  python engine/notify.py --test          # send a test alert
  python engine/notify.py --status        # show whether it is armed
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.config import Config  # noqa: E402

_last_sent: float = 0.0
_MIN_GAP = 2.0  # flood control: >=2s between messages
_err_seen: dict[str, float] = {}   # error dedupe (bitget-trading-bot merge)


def send_error_once(key: str, text: str, cooldown_s: float = 900.0) -> bool:
    """Send an error alert ONCE per cooldown window (bitget-trading-bot
    send_error_once merge: an outage that repeats every loop must become
    ONE alert, not 200). Returns True if a send actually happened."""
    import time
    now = time.time()
    if now - _err_seen.get(key, 0.0) < cooldown_s:
        return False
    _err_seen[key] = now
    return send_telegram(text)


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "")


def _chat() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "")


def telegram_enabled() -> bool:
    return bool(_token() and _chat())


def _post(payload: dict, timeout: float = 8.0) -> tuple[bool, str]:
    """POST to the Telegram Bot API. Returns (ok, error_or_empty)."""
    try:
        url = f"https://api.telegram.org/bot{_token()}/sendMessage"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
        if body.get("ok"):
            return True, ""
        return False, str(body.get("description", "telegram said no"))
    except Exception as e:  # noqa: BLE001 - never kill the loop over a ping
        return False, str(e)


def send_telegram(text: str, cfg: Config | None = None) -> bool:
    if not telegram_enabled():
        return False
    global _last_sent
    import time

    if time.time() - _last_sent < _MIN_GAP:
        return False
    _last_sent = time.time()
    ok, _ = _post({
        "chat_id": _chat(),
        "text": "\U0001fa93 GOLD RIPPER\n" + text,
        "parse_mode": "HTML",
    })
    return ok


def send_test(token: str = "", chat_id: str = "") -> tuple[bool, str]:
    """Send a verification alert. Falls back to env values."""
    if token and chat_id:
        os.environ["TELEGRAM_BOT_TOKEN"] = token
        os.environ["TELEGRAM_CHAT_ID"] = chat_id
    if not telegram_enabled():
        return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"
    snap = ""
    try:
        snap = f"\n{fmt_snapshot(Config())}"
    except Exception:  # noqa: BLE001
        pass
    return _post({
        "chat_id": _chat(),
        "text": "\U0001fa93 GOLD RIPPER\n<b>TEST ALERT</b> - the wire is "
                "live. Entries, exits and rail hits will land here."
                f"{snap}",
        "parse_mode": "HTML",
    })


def fmt_snapshot(cfg: Config) -> str:
    """One-line rails snapshot for message footers."""
    try:
        st = json.loads(Path(cfg.state_file).read_text())
        return (f"bal ${st.get('balance', 0):.2f} · "
                f"day {st.get('day_pnl', 0):+.2f} · "
                f"block {st.get('block_status', '?')}")
    except Exception:  # noqa: BLE001
        return ""


if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    ap = argparse.ArgumentParser(description="GOLD RIPPER notifier")
    ap.add_argument("--test", action="store_true", help="send a test alert")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.status:
        print("armed" if telegram_enabled() else
              "NOT armed (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env)")
        raise SystemExit(0)
    if a.test:
        ok, err = send_test()
        print("[+] test alert sent - check your Telegram" if ok
              else f"[!] send failed: {err}")
        raise SystemExit(0 if ok else 1)
    ap.print_help()
