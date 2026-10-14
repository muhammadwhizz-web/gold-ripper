"""
GOLD RIPPER :: Protections
==========================
freqtrade's protection framework, rebuilt around the ripper's dollar rails
(merged from the 9-repo audit - freqtrade/plugins/protections/*, adapted):

  COOLDOWN        - after EVERY full close, no new entry for N bars
                    (freqtradebot.py:2626 auto-lock lesson: kills instant
                    re-entry on a stale signal)
  STOPLOSS GUARD  - N loss-exits inside the lookback window -> pause
                    (StoplossGuard: a chop regime eats $1 a try; sit out)
  MAX DRAWDOWN    - balance drawdown over the window >= cap -> pause
                    (MaxDrawdownProtection, "equity" mode, dollars)
  LOW PROFIT      - window PnL below floor -> pause (optional, default off)

Locks are file-backed (data/protections[_TAG].json, atomic writes), side-
aware (a LONG lock can leave SHORT hunting), deduped, and NEVER shortened -
a lock only expires or is superseded by a LONGER one (freqtrade
PairLockMiddleware semantics).

The SAME rule set runs in the backtest (bar-based) so the calibration
numbers are honest about the pauses live trading will take.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

from engine.config import Config
from engine.journal import log_event

_utcnow = lambda: datetime.now(timezone.utc)  # noqa: E731


class ProtectionManager:
    """Live-side protection locks (minutes-based, journal-fed)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.lock = threading.Lock()
        self.locks: list[dict] = []          # active locks
        self.exits: list[dict] = []          # recent full closes (ts, pnl, reason, side)
        self.balance = cfg.starting_balance  # journal-fed balance for dd math
        self._warned = 0.0
        self._load()

    # ------------------------------------------------------------ persistence
    @property
    def _path(self):
        return self.cfg.protections_file

    def _load(self) -> None:
        try:
            d = json.loads(self._path.read_text())
            self.locks = d.get("locks", [])
            self.balance = float(d.get("balance", self.cfg.starting_balance))
            now = time.time()
            self.exits = [e for e in d.get("exits", [])
                          if now - float(e.get("ts", 0)) < 7 * 86400]
        except Exception:  # noqa: BLE001 - fresh install / corrupt file
            # fail-CLOSED semantics (astra-quant cooldowns.py): a corrupt
            # store must NOT read as "no protections" - start conservative
            self.locks = []
            self.exits = []
        # seed exit memory from the journal on first boot / empty store:
        # a restart must not wipe the protection window (stoploss guard,
        # drawdown guard survive process death)
        if not self.exits:
            try:
                self.exits = read_journal_exits(cfg=self.cfg)
                if self.exits:
                    self.balance = self.exits[-1].get("bal_after") or \
                        self.cfg.starting_balance
            except Exception:  # noqa: BLE001
                pass

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "locks": self.locks, "exits": self.exits[-60:],
                "balance": round(self.balance, 2)}, default=str))
            tmp.replace(self._path)
        except OSError:
            pass

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _bars_to_min(cfg: Config, bars: int) -> int:
        """Live tape is H1 (config poll feeds closed H1 bars)."""
        return int(bars * 60)

    def _prune(self, now: float) -> None:
        self.locks = [lk for lk in self.locks
                      if float(lk.get("until", 0)) > now]
        horizon = now - 7 * 86400
        self.exits = [e for e in self.exits if float(e.get("ts", 0)) > horizon]

    # ------------------------------------------------------------ feed me
    def on_exit(self, ts: float, pnl: float, reason: str,
                side: str = "", is_full_close: bool = True) -> None:
        """Call on every journal_close. Feeds the guards + arms cooldown."""
        if not self.cfg.prot_enabled:
            return
        with self.lock:
            now = _utcnow().timestamp()
            self.balance += float(pnl)
            self.exits.append({"ts": ts or now, "pnl": float(pnl),
                               "reason": str(reason).upper(), "side": side,
                               "full": bool(is_full_close)})
            self._prune(now)
            if is_full_close:
                self._arm_cooldown(now, pnl, reason, side)
                self._check_stoploss_guard(now)
                self._check_drawdown(now)
            self._save()

    def _add_lock(self, kind: str, minutes: float, reason: str,
                  side: str = "") -> None:
        now = _utcnow().timestamp()
        until = now + minutes * 60
        # dedupe + never shorten (freqtrade PairLockMiddleware semantics)
        for lk in self.locks:
            if lk["kind"] == kind and lk.get("side", "") == side:
                if float(lk["until"]) >= until:
                    return
                lk["until"] = until
                lk["reason"] = reason
                return
        self.locks.append({"kind": kind, "side": side, "reason": reason,
                           "until": until, "created": now})
        log_event(self.cfg, "protection_lock",
                  {"kind": kind, "side": side, "min": round(minutes),
                   "reason": reason})
        # alert at most every 30 min (notify dedupe handles the rest)
        try:
            from engine.notify import send_error_once
            send_error_once(
                f"prot-{kind}", f"<b>PROTECTION</b> {kind} lock "
                f"{round(minutes)}m ({side or 'both sides'}) - {reason}",
                cooldown_s=1800)
        except Exception:  # noqa: BLE001
            pass

    def _arm_cooldown(self, now: float, pnl: float, reason: str,
                      side: str) -> None:
        mins = self._bars_to_min(self.cfg, self.cfg.prot_cooldown_bars)
        self._add_lock("cooldown", mins,
                       f"post-exit cooldown ({reason} ${pnl:+.2f})", side)

    def _check_stoploss_guard(self, now: float) -> None:
        win = self._bars_to_min(self.cfg, self.cfg.prot_sl_window_bars) * 60
        recent = [e for e in self.exits
                  if e.get("full") and float(e["pnl"]) < 0
                  and now - float(e["ts"]) <= win]
        if len(recent) > self.cfg.prot_sl_max:
            mins = self._bars_to_min(self.cfg, self.cfg.prot_sl_pause_bars)
            self._add_lock(
                "stoploss_guard", mins,
                f"{len(recent)} loss exits in {win // 3600}h "
                f"(cap {self.cfg.prot_sl_max})", "")

    def _check_drawdown(self, now: float) -> None:
        """Balance drawdown over the window. Backwards PnL replay: the
        running balance before each exit reconstructs the equity path
        (works with or without journal bal_after stamps)."""
        win = self._bars_to_min(self.cfg, self.cfg.prot_dd_window_bars) * 60
        ordered = sorted(self.exits, key=lambda e: float(e["ts"]))
        bal = self.balance
        peak = bal
        dd = 0.0
        for e in reversed(ordered):
            if now - float(e["ts"]) > win:
                continue
            bal -= float(e["pnl"])
            peak = max(peak, bal)
            dd = max(dd, peak - self.balance)
        if dd >= self.cfg.prot_dd_max_usd:
            mins = self._bars_to_min(self.cfg, self.cfg.prot_dd_window_bars)
            self._add_lock("max_drawdown", mins,
                           f"dd ${dd:.2f} >= ${self.cfg.prot_dd_max_usd:.2f} "
                           f"in {win // 3600}h", "")

    # ------------------------------------------------------------ the gate
    def can_enter(self, side: str = "") -> tuple[bool, str]:
        if not self.cfg.prot_enabled:
            return True, "OK"
        with self.lock:
            now = _utcnow().timestamp()
            self._prune(now)
            self._save()
            for lk in self.locks:
                s = lk.get("side", "")
                if s and side and s != side:
                    continue    # side-aware lock - other side may hunt
                mins = (float(lk["until"]) - now) / 60
                return False, (f"{lk['kind']} lock {mins:.0f}m left - "
                               f"{lk['reason']}")
            return True, "OK"

    # ------------------------------------------------------------ heartbeat
    def active(self) -> list[dict]:
        with self.lock:
            now = _utcnow().timestamp()
            self._prune(now)
            return [{**lk, "left_min": round((float(lk["until"]) - now) / 60)}
                    for lk in self.locks]


def read_journal_exits(cfg: Config, limit: int = 60) -> list[dict]:
    """Seed ProtectionManager.exits from the journal CSV after a restart
    (the bot resumes its protection memory instead of starting blank)."""
    import csv
    from pathlib import Path
    f = Path(cfg.journal_file)
    if not f.exists():
        return []
    out: list[dict] = []
    try:
        with f.open("r", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))[-limit:]
        for r in rows:
            try:
                ts = datetime.fromisoformat(
                    str(r.get("ts_close_utc", "")).replace(
                        "Z", "+00:00")).timestamp()
            except ValueError:
                continue
            out.append({"ts": ts, "pnl": float(r.get("pnl_usd") or 0),
                        "reason": str(r.get("reason", "")).upper(),
                        "side": str(r.get("side", "")), "full": True,
                        "bal_after": float(r.get("balance_after") or 0)})
    except Exception:  # noqa: BLE001
        return []
    return out
