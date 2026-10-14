"""
GOLD RIPPER :: News Guard
=========================
Pulls this week's economic calendar (ForexFactory weekly JSON) and blocks
NEW entries inside +-blackout minutes of high-impact gold movers
(NFP, CPI, FOMC, PPI, GDP, rate decisions, Powell speeches).

Fail-open philosophy: if the calendar cannot be fetched, the guard logs
and allows trading (the rails still protect the account). Fail-closed
would leave the bot silent forever - the rails are the real protection.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime, timezone

from engine.config import Config
from engine.journal import log_event

GOLD_CRITICAL = ("non-farm", "nonfarm", "cpi", "fomc", "federal funds",
                 "ppi", "gdp", "unemployment claims", "powell", "fed chair",
                 "core retail", "ism manufacturing")

PULL_JOIN_S = 3.0   # max the loop ever waits for a fresh calendar
RETRY_S = 300.0     # after a failed pull, lie low 5 min (never hammer)


def _epoch(v) -> float:
    """ForexFactory 'date' flips between epoch-ms numbers and ISO-8601
    strings between weeks ('2026-10-14T08:30:00-04:00'). Return epoch
    seconds, or 0 when unparsable (fail-open, never kill the pull)."""
    if v in (None, ""):
        return 0.0
    try:
        f = float(v)
        return f / 1000.0 if f > 1e11 else f
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(
                str(v).replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            return 0.0


class NewsGuard:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.events: list[dict] = []      # [{ts(epoch), name, impact}]
        self.last_pull = 0.0
        # HARDENING: urllib's timeout does NOT cover DNS/connect stalls -
        # a slow calendar endpoint froze the whole trading loop (seen in
        # the sandbox). Pulls now run in a daemon thread with a hard cap;
        # the loop trades on the last good calendar until the new one lands.
        self._lock = threading.Lock()
        self.last_attempt = 0.0
        self.last_error: str | None = None

    def _pull(self) -> None:
        try:
            req = urllib.request.Request(
                self.cfg.news_url,
                headers={"User-Agent": "gold-ripper/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            events = []
            for ev in data:
                if str(ev.get("impact", "")).lower() not in ("high", "medium"):
                    continue
                title = str(ev.get("title", "")).lower()
                if str(ev.get("country", "")).upper() != "USD":
                    continue
                if any(k in title for k in GOLD_CRITICAL):
                    events.append({
                        "ts": _epoch(ev.get("date")),
                        "name": ev.get("title", "?"),
                        "impact": ev.get("impact", "?"),
                    })
            self.events = [e for e in events if e["ts"] > 0]
            self.last_pull = time.time()
            self.last_error = None
            log_event(self.cfg, "news_pull",
                      {"events": len(self.events)})
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)[:120]
            log_event(self.cfg, "news_pull_failed", {"error": str(e)})
        finally:
            try:
                self._lock.release()
            except Exception:  # noqa: BLE001
                pass

    def refresh(self, max_age_min: int = 180) -> None:
        """Fire-(almost)-forget refresh. The loop waits at most PULL_JOIN_S
        for the thread (usually it lands in ~1s); a hung DNS/connect stall
        costs the loop NOTHING - the calendar lands on a later poll."""
        now = time.time()
        if now - self.last_pull <= max_age_min * 60:
            return
        if now - self.last_attempt < RETRY_S:
            return
        if not self._lock.acquire(blocking=False):
            return  # a pull is already in flight
        self.last_attempt = now
        threading.Thread(target=self._pull, daemon=True,
                         name="ripper-news").start()
        deadline = now + PULL_JOIN_S
        while self._lock.locked() and time.time() < deadline:
            time.sleep(0.05)

    def blocked(self, now: datetime | None = None) -> tuple[bool, str]:
        if not self.cfg.news_guard:
            return False, ""
        now = now or datetime.now(timezone.utc)
        self.refresh()
        win = self.cfg.news_blackout_min * 60
        for e in self.events:
            if abs(now.timestamp() - e["ts"]) <= win:
                return True, f"news blackout: {e['name']}"
        return False, ""

    def next_event(self) -> dict | None:
        now = datetime.now(timezone.utc).timestamp()
        future = [e for e in self.events if e["ts"] > now]
        return min(future, key=lambda e: e["ts"]) if future else None
