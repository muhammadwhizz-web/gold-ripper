"""
GOLD RIPPER :: Backtest Engine
==============================
Bar-by-bar replay whose execution semantics are IDENTICAL to the
validated probe that found the edge (and to ripper.py's live ladder):

  signal on bar i (closed bar)
  -> entry at the close of bar i+1, cost-adjusted (taker fee + slippage)
  -> SL: venue-side stop, checked first every bar (conservative)
  -> TP1 at tp1_r banks tp1_fraction of the position, stop -> breakeven
  -> runner trails by ATR chandelier from the very next bar (ratchet)
  -> time-stop closes at market after time_stop_bars

  THE RAILS are enforced exactly like live:
    $1 risk/trade, 3 attempts per 4h block, block stops at +$20/-$3,
    day stops at +$120/-$5, consecutive-loss cooldown.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from engine import assurance
from engine.config import Config
from engine.strategy import Position, StrategyEngine, Signal

# warmup: the daily-bias EMA20 needs ~20 daily bars (~480 H1 bars) before
# it means anything (passivbot warmup lesson: never trade inside the
# indicator warmup window). 500 H1 bars = safe floor for every ctx series.
WARMUP_BARS = 500


@dataclass
class BTResult:
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    blocks: dict = field(default_factory=dict)
    days: dict = field(default_factory=dict)


def _block_key(ts) -> str:
    return f"{ts:%Y-%m-%dT}{(ts.hour // 4) * 4:02d}"


def _day_key(ts) -> str:
    return f"{ts:%Y-%m-%d}"


def run_backtest(df: pd.DataFrame, cfg: Config, start=None, end=None,
                 engine: StrategyEngine | None = None) -> BTResult:
    eng = engine or StrategyEngine(cfg)
    cost = cfg.fee_rate + cfg.slippage_rate
    data = df.loc[(df.index >= start) & (df.index <= end)] if start or end else df
    ctx = eng._context(data)
    idx = data.index
    H = data["high"].to_numpy()
    L = data["low"].to_numpy()
    C = data["close"].to_numpy()
    A = ctx["atr"].to_numpy()

    res = BTResult()
    balance = cfg.starting_balance

    # rails state
    block_key_cur = ""
    block_pnl = block_attempts = 0
    block_done = False
    day_key_cur = ""
    day_pnl = 0.0
    day_attempts = 0
    day_done = False
    consec_losses = 0
    pause_until = None

    # pending order (signal fired on previous bar -> fill this bar) / position
    pend: dict | None = None
    pos: dict | None = None

    # protections state (freqtrade merge - same rules as live side)
    close_events: list = []   # (i, pnl, balance_after) full closes only
    last_full_close_i: int | None = None

    def register(pnl: float, ts, row: dict | None = None,
                 row_pnl: float | None = None):
        nonlocal balance, consec_losses, block_pnl, day_pnl
        nonlocal last_full_close_i
        balance += pnl
        bk, dk = _block_key(ts), _day_key(ts)
        res.blocks[bk] = res.blocks.get(bk, 0.0) + pnl
        res.days[dk] = res.days.get(dk, 0.0) + pnl
        if row is not None:
            row["pnl"] = round(row_pnl if row_pnl is not None else pnl, 2)
            row["balance"] = round(balance, 2)
            res.trades.append(row)
            # full close -> protection memory (cooldown / guards feed)
            close_events.append((i_reg[0], pnl, balance))
            last_full_close_i = i_reg[0]
        if pnl < 0:
            consec_losses += 1
        elif pnl > 0:
            consec_losses = 0

    i_reg = [61]  # current bar index, set every loop (closure-friendly)

    n = len(data)
    for i in range(WARMUP_BARS, n):
        ts = idx[i]
        i_reg[0] = i
        bk, dk = _block_key(ts), _day_key(ts)
        if bk != block_key_cur:
            block_key_cur, block_pnl, block_attempts, block_done = bk, 0, 0, False
        if dk != day_key_cur:
            day_key_cur, day_pnl, day_done, day_attempts = dk, 0.0, False, 0

        # ── 1. fill pending order (maker limit at signal close, or taker)
        if pend is not None and pos is None:
            sig = pend["sig"]
            side = sig.side
            if cfg.maker_entry:
                # post-only limit at the signal close; fills when touched
                if i - pend["i"] > cfg.maker_wait_bars:
                    pend = None
                else:
                    touched = (L[i] <= pend["px"]) if side == "LONG" \
                        else (H[i] >= pend["px"])
                    if not touched:
                        continue
                    entry = pend["px"] * (1 - cfg.fee_maker) if side == "LONG" \
                        else pend["px"] * (1 + cfg.fee_maker)
                    pend_px_ref = pend["ref"]
                    pend = None
            else:
                pend_px_ref = pend["ref"]
                pend = None
                entry = C[i] * (1 + cost) if side == "LONG" else C[i] * (1 - cost)
            # probe-exact: stop is ENTRY-relative, sized by the SIGNAL-bar
            # risk distance (module sl vs signal close = e.g. 4.5xATR)
            sl_dist = abs(pend_px_ref - float(sig.sl))
            if sl_dist <= 0:
                continue
            if side == "LONG":
                sl = entry - sl_dist
                tp1 = entry + sig.tp1_r * sl_dist
            else:
                sl = entry + sl_dist
                tp1 = entry - sig.tp1_r * sl_dist
            risk_dist = sl_dist
            if risk_dist > 0 and block_attempts < cfg.max_attempts_per_block \
                    and not block_done and not day_done:
                size = cfg.risk_usd_per_trade / risk_dist
                size = min(size, balance * 3 / entry)
                pos = {
                    "side": side, "entry": entry, "sl": sl, "orig_sl": sl,
                    "tp1": tp1, "size": size, "remaining": 1.0,
                    "module": sig.module, "i0": i, "tp1_done": False,
                    "trail_mult": sig.trail_atr_mult, "tstop": sig.time_stop_bars,
                    "ts_open": str(idx[i - 1]), "reason": sig.reason,
                    "acc_pnl": 0.0,
                }
                block_attempts += 1
                day_attempts += 1

        # ── 2. manage open position (from the bar AFTER the fill onward;
        #        the fill bar's own high/low happened before the close-entry)
        if pos is not None and i > pos["i0"]:
            a = max(float(A[i]), 1e-6)
            side = pos["side"]
            exit_px = None
            reason = None

            if side == "LONG" and L[i] <= pos["sl"]:
                exit_px, reason = pos["sl"] * (1 - cost), "SL"
            elif side == "SHORT" and H[i] >= pos["sl"]:
                exit_px, reason = pos["sl"] * (1 + cost), "SL"
            elif not pos["tp1_done"]:
                if (side == "LONG" and H[i] >= pos["tp1"]) or \
                        (side == "SHORT" and L[i] <= pos["tp1"]):
                    # bank TP1 portion
                    pnl_tp1 = pos["size"] * cfg.tp1_fraction * pos["remaining"] \
                        * ((pos["tp1"] - pos["entry"]) *
                           (1 if side == "LONG" else -1)) \
                        - pos["size"] * cfg.tp1_fraction * pos["remaining"] \
                        * (pos["tp1"] + pos["entry"]) * cost
                    pos["acc_pnl"] += pnl_tp1
                    register(pnl_tp1, ts)
                    pos["remaining"] *= (1 - cfg.tp1_fraction)
                    pos["tp1_done"] = True
                    pos["sl"] = pos["entry"]  # breakeven
            if exit_px is None and pos is not None:
                if (i - pos["i0"]) >= pos["tstop"]:
                    exit_px = C[i] * ((1 - cost) if side == "LONG"
                                      else (1 + cost))
                    reason = "TIME"
                elif pos["tp1_done"] or cfg.trail_start_r <= 0:
                    # ATR chandelier ratchet, every bar
                    if side == "LONG":
                        trail = H[i] - pos["trail_mult"] * a
                        if trail > pos["sl"]:
                            pos["sl"] = trail
                        if L[i] <= pos["sl"]:
                            exit_px = pos["sl"] * (1 - cost)
                            reason = "TRAIL"
                    else:
                        trail = L[i] + pos["trail_mult"] * a
                        if trail < pos["sl"]:
                            pos["sl"] = trail
                        if H[i] >= pos["sl"]:
                            exit_px = pos["sl"] * (1 + cost)
                            reason = "TRAIL"

            if exit_px is not None:
                direction = 1 if side == "LONG" else -1
                gross = pos["size"] * pos["remaining"] * \
                    (exit_px - pos["entry"]) * direction
                fees = pos["size"] * pos["remaining"] * \
                    (exit_px + pos["entry"]) * cost
                pnl = gross - fees
                total = pos["acc_pnl"] + pnl
                register(pnl, ts, {
                    "ts_open": pos["ts_open"], "ts_close": str(ts),
                    "side": side, "module": pos["module"],
                    "entry": round(pos["entry"], 2), "exit": round(exit_px, 2),
                    "reason": reason}, row_pnl=total)
                pos = None

        res.equity_curve.append({"ts": str(ts), "equity": round(balance, 2)})

        # ── 3. rails gate + new signal (fires for NEXT bar's close)
        if pos is not None or pend is not None:
            continue
        # ── protections (freqtrade merge): cooldown / stoploss guard /
        #    drawdown guard - the SAME rules engine/protections.py runs
        #    live, so the calibration is honest about the pauses.
        if cfg.prot_enabled:
            if last_full_close_i is not None and \
                    i - last_full_close_i < cfg.prot_cooldown_bars:
                continue
            win = cfg.prot_sl_window_bars
            n_loss = sum(1 for (ci, p, _b) in close_events
                         if i - ci <= win and p < 0)
            if n_loss > cfg.prot_sl_max:
                continue
            dd_win = cfg.prot_dd_window_bars
            run_peak, run_dd = balance, 0.0
            for (ci, _p, bal) in close_events:
                if i - ci <= dd_win and bal > 0:
                    run_peak = max(run_peak, bal)
            run_dd = run_peak - balance
            if run_dd >= cfg.prot_dd_max_usd:
                continue
        if block_done or day_done:
            continue
        if block_attempts >= cfg.max_attempts_per_block:
            block_done = True
            continue
        if block_pnl <= -cfg.block_max_loss_usd:
            block_done = True
            continue
        if day_pnl <= -cfg.day_max_loss_usd:
            day_done = True
            continue
        if pause_until is not None and ts < pause_until:
            continue
        if consec_losses >= cfg.consec_loss_pause_after:
            pause_until = ts + pd.Timedelta(minutes=cfg.consec_loss_pause_min)
            consec_losses = 0
            continue

        # daily assurance: SAME rules as live (engine/assurance.py) -
        # late-day gate relaxation when the day has produced no fill yet,
        # so calibration is honest about assured entries
        lvl = assurance.level(ts.to_pydatetime(), day_attempts,
                              pend is not None, cfg)
        sig = eng.evaluate(data, ctx, i, ts, assure=lvl)
        if sig is None:
            continue
        pend = {"sig": sig, "i": i, "px": float(C[i]), "ref": float(C[i])}

    if pos is not None:
        i = n - 1
        side = pos["side"]
        exit_px = C[i] * ((1 - cost) if side == "LONG" else (1 + cost))
        direction = 1 if side == "LONG" else -1
        pnl = pos["size"] * pos["remaining"] * (exit_px - pos["entry"]) * \
            direction - pos["size"] * pos["remaining"] * \
            (exit_px + pos["entry"]) * cost
        register(pnl, idx[i], {
            "ts_open": pos["ts_open"], "ts_close": str(idx[i]),
            "side": side, "module": pos["module"],
            "entry": round(pos["entry"], 2), "exit": round(exit_px, 2),
            "reason": "EOD"})

    return res


def summarize(res: BTResult, cfg: Config) -> dict:
    tr = res.trades
    n = len(tr)
    if n == 0:
        return {"trades": 0}
    pnls = [t["pnl"] for t in tr]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp, gl = sum(wins), abs(sum(losses))
    blocks = list(res.blocks.values())
    days = list(res.days.values())
    eq = [p["equity"] for p in res.equity_curve]
    peak, maxdd = eq[0] if eq else cfg.starting_balance, 0.0
    for e in eq:
        peak = max(peak, e)
        maxdd = min(maxdd, (e - peak) / peak)
    return {
        "trades": n,
        "win_rate": round(100 * len(wins) / n, 1),
        "net": round(sum(pnls), 2),
        "profit_factor": round(gp / gl, 2) if gl else None,
        "avg_win": round(gp / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gl / len(losses), 2) if losses else 0.0,
        "max_drawdown_pct": round(maxdd * 100, 2),
        "final_balance": round(eq[-1], 2) if eq else cfg.starting_balance,
        "blocks_total": len(blocks),
        "blocks_hit_target": sum(1 for b in blocks if b >= cfg.block_target_usd),
        "blocks_stopped_loss": sum(1 for b in blocks if b <= -cfg.block_max_loss_usd),
        "days_total": len(days),
        "days_hit_target": sum(1 for d in days if d >= cfg.day_target_usd),
        "days_loss_capped": sum(1 for d in days if d <= -cfg.day_max_loss_usd),
    }
