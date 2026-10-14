"""
GOLD RIPPER :: Venue reconciliation (LIVE)
==========================================
Every venue fill the ladder missed can never silently diverge.

In LIVE mode the venue itself executes parts of the plan without the bot's
help: venue-side SL/TP algo orders fire between polls, a maker limit fills
while the feed hiccups, a manual position is closed from the Bitget app.
The software loop only sees the aftermath. This module periodically diffs
the bot's journal against Bitget's OWN fill history (fetch_my_trades) so
the two ledgers must agree - or the dashboard and Telegram hear about it.

Report (rides the heartbeat "reconcile" field):
  status OK      every venue ENTRY fill has a matching journal row
  status GAP     venue filled an entry the journal never recorded
                 (CRITICAL - Telegram alert, cooldown-guarded)
  status ERROR   venue unreachable this cycle (last-good report is kept)
  status SKIP    paper mode / no broker support - nothing to reconcile

Matching rule (deliberately simple, explainable in an audit):
  a venue ENTRY fill (reduce-only=False) matches the oldest journal row
  with the same side whose open timestamp is within +-6h of the fill and
  that has not been matched yet. Venue-side EXIT fills (reduce-only=True)
  are counted but never expected in the journal - the venue algo legs
  close positions by design; they are reported so the money is visible.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

WINDOW_H = 6          # journal-open <-> venue-fill match tolerance, hours
MAX_FILLS = 200       # fetch_my_trades page size per reconcile cycle


def state_file(cfg) -> Path:
    return getattr(cfg, "reconcile_state_file", None) or (
        cfg.heartbeat_file.parent / "reconcile.json")


def _read_state(cfg) -> dict:
    try:
        return json.loads(state_file(cfg).read_text())
    except Exception:  # noqa: BLE001
        return {}


def _write_state(cfg, st: dict) -> None:
    try:
        p = state_file(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, default=str))
        tmp.replace(p)
    except Exception:  # noqa: BLE001 - never kill the loop over bookkeeping
        pass


def _iso_to_epoch(s) -> float:
    if not s:
        return 0.0
    try:
        from datetime import datetime, timezone
        s = str(s).replace("Z", "+00:00").replace(" ", "T")
        if "+" not in s and not s.endswith("Z") and s.count("-") >= 2:
            s = s + "+00:00"  # naive stamps are UTC
        return datetime.fromisoformat(s).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def run(cfg, broker, force: bool = False) -> dict:
    """One reconciliation cycle. Returns the report dict (heartbeat-carried).

    force=True ignores the interval (used by an explicit operator check).
    Paper mode returns SKIP - there is no venue truth to diff against.
    """
    fills_fn = getattr(broker, "recent_fills", None)
    if cfg.mode != "LIVE" or not callable(fills_fn):
        return {"status": "SKIP",
                "note": f"{cfg.mode} mode - no venue fill history to diff",
                "ts": time.time()}

    st = _read_state(cfg)
    last_run = float(st.get("last_run") or 0)
    now = time.time()
    if not force and now - last_run < cfg.reconcile_min * 60:
        rep = dict(st.get("report") or {})
        if rep:
            rep["age_min"] = round((now - float(rep.get("ts") or now)) / 60)
        return rep

    since = float(st.get("last_fill_ts") or (now - 24 * 3600))
    since_iso = st.get("last_iso")
    rep: dict = {
        "status": "ERROR", "ts": now, "since_iso": since_iso,
        "venue_fills_n": 0, "entry_n": 0, "exit_n": 0, "matched_n": 0,
        "unmatched": [], "note": "",
    }
    try:
        fills = fills_fn(since_iso) or []
    except Exception as e:  # noqa: BLE001 - venue down, keep last report
        rep["status"] = "ERROR"
        rep["note"] = f"fetch_my_trades failed: {str(e)[:120]}"
        _write_state(cfg, {"last_run": now, "report": rep})
        return rep

    if not fills:
        rep["status"] = "OK"
        rep["note"] = "no venue fills in window"
        rep["ts"] = now
        _write_state(cfg, {"last_run": now, "report": rep,
                           "last_iso": since_iso, "last_fill_ts": since})
        return rep

    # ---- venue side: split entries from venue-side algo exits ----------
    entries = [f for f in fills if not f.get("reduce_only")]
    exits = [f for f in fills if f.get("reduce_only")]
    rep["venue_fills_n"] = len(fills)
    rep["entry_n"] = len(entries)
    rep["exit_n"] = len(exits)

    # ---- journal side: live rows in (roughly) the same window ----------
    journal = _read_journal_rows(cfg)
    j_open = []
    for r in journal:
        t0 = _iso_to_epoch(r.get("ts_open"))
        if t0 and t0 >= since - WINDOW_H * 3600:
            j_open.append({"side": str(r.get("side", "")).upper(),
                           "ts": t0, "row": r})
    j_open.sort(key=lambda x: x["ts"])

    # ---- match: oldest venue entry <-> oldest same-side journal open ---
    unmatched = []
    used = set()
    matched_rows = []  # journal rows a venue entry was matched to (exit pricing)
    for f in sorted(entries, key=lambda x: float(x.get("ts") or 0)):
        fts = float(f.get("ts") or 0)
        fside = str(f.get("side", "")).upper()  # BUY -> LONG
        jside = "LONG" if fside == "BUY" else "SHORT"
        hit = None
        for j in j_open:
            if id(j) in used or j["side"] != jside:
                continue
            if abs(j["ts"] - fts) <= WINDOW_H * 3600:
                hit = j
                break
        if hit:
            used.add(id(hit))
            matched_rows.append(hit)
        else:
            unmatched.append({
                "iso": f.get("iso"), "side": fside,
                "px": f.get("px"), "amount": f.get("amount"),
                "oid": str(f.get("oid") or "")[:18]})
    rep["matched_n"] = len(entries) - len(unmatched)
    rep["unmatched"] = unmatched[:8]
    rep["unmatched_n"] = len(unmatched)

    # ---- venue-side exits: fee-weighted PnL ESTIMATE -------------------
    # The venue algo legs (SL/TP) close positions by design; the journal
    # never records them. To make the money visible we price each exit
    # against the nearest matched journal entry of the implied side:
    #   SELL exit -> closes a LONG  -> (exit_px - entry_px) * amount
    #   BUY  exit -> closes a SHORT -> (entry_px - exit_px) * amount
    # minus the fill fee (ccxt reports it signed, negative = paid). This is
    # an ESTIMATE - the truth lives in Bitget's own wallet history - but it
    # turns "3 algo exits happened" into "algo exits banked about +$14".
    fees_est = 0.0
    exit_pnl: float | None = 0.0 if exits else None
    exit_detail = []
    if exits:
        for x in sorted(exits, key=lambda x: float(x.get("ts") or 0)):
            fees_est += abs(float(x.get("fee") or 0))
            xside = str(x.get("side", "")).upper()  # SELL closes LONG
            want = "LONG" if xside == "SELL" else "SHORT"
            xts = float(x.get("ts") or 0)
            basis = None
            best_dt = None
            for m in matched_rows:
                if m["side"] != want:
                    continue
                dt = abs(m["ts"] - xts)
                if best_dt is None or dt < best_dt:
                    best_dt = dt
                    basis = m
            if basis is None:
                exit_pnl = None  # no pricing basis - refuse to guess
                continue
            try:
                # journal column is "entry" (schema v1/v2)
                entry_px = float(basis["row"].get("entry") or 0)
                xpx = float(x.get("px") or 0)
                amt = float(x.get("amount") or 0)
                fee = float(x.get("fee") or 0)
                if entry_px <= 0 or xpx <= 0 or amt <= 0:
                    continue
                direction = 1.0 if want == "LONG" else -1.0
                exit_pnl += (xpx - entry_px) * direction * amt + fee
                exit_detail.append(
                    f"{x.get('iso', '?')} {xside} {amt:g} @ {xpx:g}")
            except Exception:  # noqa: BLE001 - estimate must never throw
                continue
    for f in entries:
        fees_est += abs(float(f.get("fee") or 0))
    rep["fees_est"] = round(fees_est, 2)
    rep["exit_pnl_est"] = (round(exit_pnl, 2)
                           if exit_pnl is not None else None)
    rep["exit_detail"] = exit_detail[:8]

    if unmatched:
        rep["status"] = "GAP"
        rep["note"] = (f"{len(unmatched)} venue entry fill(s) with no "
                       f"journal row - the ladder missed a fill or the "
                       f"position was managed outside the bot")
    else:
        rep["status"] = "OK"
        rep["note"] = f"{len(entries)} entry fill(s) matched"
        if exits:
            pnl = rep.get("exit_pnl_est")
            tail = (f"est ${pnl:+.2f}" if pnl is not None
                    else "no matched entry to price against")
            rep["note"] += (f" · {len(exits)} venue-side exit(s) "
                            f"(algo SL/TP) {tail}")

    # advance the window cursor to the newest fill we have seen
    newest = max((float(f.get("ts") or 0) for f in fills), default=since)
    rep["ts"] = now
    _write_state(cfg, {"last_run": now, "report": rep,
                       "last_iso": fills[-1].get("iso") if fills else since_iso,
                       "last_fill_ts": max(newest, since)})
    return rep


def _read_journal_rows(cfg) -> list[dict]:
    """Live journal rows (list of dicts) - tolerant of schema v1/v2."""
    try:
        lines = cfg.journal_file.read_text().strip().split("\n")
    except Exception:  # noqa: BLE001
        return []
    if len(lines) < 2:
        return []
    header = [h.strip() for h in lines[0].split(",")]
    out = []
    for ln in lines[1:]:
        cols = ln.split(",")
        if len(cols) < len(header):
            continue
        out.append({h: cols[i] for i, h in enumerate(header)})
    return [r for r in out if str(r.get("mode", "")).upper() == "LIVE"]


def alert_text(rep: dict) -> str:
    """Telegram body for a GAP verdict."""
    rows = "\n".join(
        f"  • {u.get('iso', '?')} {u.get('side')} {u.get('amount')} @ "
        f"{u.get('px')}" for u in (rep.get("unmatched") or [])[:5])
    return ("<b>RECONCILE GAP</b> - Bitget shows fills the journal never "
            f"recorded\n{rows}\nCheck the venue order history; the rails "
            "may be blind to this exposure.")
