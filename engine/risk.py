"""
GOLD RIPPER :: Risk Rails
=========================
The user's mission, enforced mechanically. The ripper never negotiates:

  4h BLOCK (UTC-aligned):
    +$20  -> block marked DONE, no more entries until next block
    -$3   -> block marked DEAD, no more entries until next block
    max 3 attempts per block

  DAY (UTC):
    +$120 -> done for the day
    -$5   -> done for the day (total, across blocks)

  Streaks: 2 consecutive losses -> 45min cooldown (configurable)
  Sizing : risk_usd_per_trade per trade (default $1) so 3 stops = -$3 cap
           exactly. Adaptive: risk scales with equity, capped.

State persists to data/ripper_state.json and survives restarts.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from engine.config import Config

_utcnow = lambda: datetime.now(timezone.utc)  # noqa: E731


def block_key(ts=None) -> str:
    ts = ts or _utcnow()
    return f"{ts:%Y-%m-%dT}{(ts.hour // 4) * 4:02d}"


def day_key(ts=None) -> str:
    ts = ts or _utcnow()
    return f"{ts:%Y-%m-%d}"


@dataclass
class RiskState:
    balance: float = 1000.0
    equity: float = 1000.0
    block: str = ""
    block_pnl: float = 0.0
    block_attempts: int = 0
    block_status: str = "OPEN"          # OPEN | DONE | DEAD
    day: str = ""
    day_pnl: float = 0.0
    day_status: str = "OPEN"            # OPEN | DONE | DEAD
    day_attempts: int = 0               # fills today (assurance input)
    consec_losses: int = 0
    pause_until: float = 0.0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0


class RiskRails:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.lock = threading.Lock()
        self.state = RiskState()
        self._size_eq: float | None = None   # hysteresis sizing equity
        self.load()

    # ------------------------------------------------------------ persistence
    def load(self) -> None:
        try:
            raw = json.loads(self.cfg.state_file.read_text())
            self.state = RiskState(**{k: raw.get(k, getattr(self.state, k))
                                      for k in self.state.__dataclass_fields__})
        except Exception:  # noqa: BLE001
            self.state = RiskState(
                balance=self.cfg.starting_balance,
                equity=self.cfg.starting_balance)
        self._roll()

    def save(self) -> None:
        try:
            self.cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cfg.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self.state), indent=2))
            tmp.replace(self.cfg.state_file)
        except OSError:
            pass

    # ------------------------------------------------------------ period rolls
    def _roll(self) -> None:
        st = self.state
        now = _utcnow()
        if st.block != block_key(now):
            st.block = block_key(now)
            st.block_pnl = 0.0
            st.block_attempts = 0
            st.block_status = "OPEN"
        if st.day != day_key(now):
            st.day = day_key(now)
            st.day_pnl = 0.0
            st.day_status = "OPEN"
            st.day_attempts = 0
            st.consec_losses = 0

    # ------------------------------------------------------------ the gate
    def can_enter(self) -> tuple[bool, str]:
        with self.lock:
            self._roll()
            st = self.state
            if st.block_status == "DONE":
                return False, f"block target hit +${st.block_pnl:.2f} - resting"
            if st.block_status == "DEAD":
                return False, f"block loss cap hit -${abs(st.block_pnl):.2f} - dead"
            if st.day_status == "DONE":
                return False, f"day target hit +${st.day_pnl:.2f} - done"
            if st.day_status == "DEAD":
                return False, f"day loss cap hit -${abs(st.day_pnl):.2f} - done"
            if st.block_attempts >= self.cfg.max_attempts_per_block:
                st.block_status = "DEAD"
                return False, "block attempt budget spent"
            if st.pause_until > _utcnow().timestamp():
                mins = (st.pause_until - _utcnow().timestamp()) / 60
                return False, f"streak cooldown {mins:.0f}m left"
            return True, "OK"

    # ------------------------------------------------------------ sizing
    def _sizing_equity(self) -> float:
        """2% hysteresis snap (passivbot utils.rs:172 merge): the sizing
        balance only moves when equity actually moved >=2% - funding and
        micro-PnL jitter must not re-derive order sizes every poll."""
        eq = max(self.state.equity, self.cfg.starting_balance)
        if self._size_eq is None or \
                abs(eq / max(self._size_eq, 1e-9) - 1.0) >= 0.02:
            self._size_eq = eq
        return self._size_eq

    def size_for(self, entry: float, sl: float) -> tuple[float, float]:
        """(units, risk_usd). risk fixed by rails; notional capped 3x equity."""
        dist = abs(entry - sl)
        if dist <= 0:
            return 0.0, 0.0
        equity = self._sizing_equity()
        # scale risk a little with equity but never exceed the rail family
        risk = min(self.cfg.risk_usd_per_trade * (equity / self.cfg.starting_balance),
                   self.cfg.max_risk_usd_per_trade)
        # ensure 3 full stops can never breach the block cap
        risk = min(risk, self.cfg.block_max_loss_usd /
                   max(1, self.cfg.max_attempts_per_block))
        units = risk / dist
        max_units = (equity * 3.0) / entry
        units = min(units, max_units)
        return round(units, 4), risk

    # ------------------------------------------------------------ fills
    def register_attempt(self) -> None:
        with self.lock:
            self._roll()
            self.state.block_attempts += 1
            self.state.day_attempts += 1
            self.save()

    def register_close(self, pnl: float, is_full_close: bool) -> None:
        with self.lock:
            self._roll()
            st = self.state
            st.balance += pnl
            st.equity = st.balance
            st.total_pnl += pnl
            st.block_pnl += pnl
            st.day_pnl += pnl
            if pnl > 0:
                st.wins += 1
                st.consec_losses = 0
            elif pnl < 0:
                st.losses += 1
                st.consec_losses += 1
                if st.consec_losses >= self.cfg.consec_loss_pause_after:
                    st.pause_until = (_utcnow().timestamp() +
                                      self.cfg.consec_loss_pause_min * 60)
            if is_full_close:
                if st.block_pnl >= self.cfg.block_target_usd:
                    st.block_status = "DONE"
                elif st.block_pnl <= -self.cfg.block_max_loss_usd:
                    st.block_status = "DEAD"
                if st.day_pnl >= self.cfg.day_target_usd:
                    st.day_status = "DONE"
                elif st.day_pnl <= -self.cfg.day_max_loss_usd:
                    st.day_status = "DEAD"
            self.save()

    def sync_equity(self, live_equity: float) -> None:
        if live_equity and live_equity > 0:
            self.state.equity = live_equity
            self.save()

    def snapshot(self) -> dict:
        with self.lock:
            self._roll()
            st = self.state
            return {
                "balance": round(st.balance, 2),
                "equity": round(st.equity, 2),
                "block": st.block,
                "block_pnl": round(st.block_pnl, 2),
                "block_attempts": st.block_attempts,
                "block_status": st.block_status,
                "day_pnl": round(st.day_pnl, 2),
                "day_status": st.day_status,
                "day_attempts": st.day_attempts,
                "wins": st.wins,
                "losses": st.losses,
                "total_pnl": round(st.total_pnl, 2),
                "cooldown_until": st.pause_until,
            }
