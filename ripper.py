#!/usr/bin/env python3
"""
██╗  ██╗ ██████╗ ██╗     ██████╗
██║  ██║██╔════╝ ██║    ██╔════╝
███████║██║  ███╗██║    ██║
██║  ██║██║   ██║██║    ██║
██║  ██║╚██████╔╝███████╗╚██████╗
╚═╝  ╚═╝ ╚═════╝ ╚══════╝ ╚═════╝
GOLD RIPPER :: 24/7 XAU hunter on Bitget
=========================================
Rails enforced every second of every day:
  +$20 per 4h block / -$3 block kill · +$120 per day / -$5 day kill
  3 attempts per block · $1 risk per trade · streak cooldowns
  news blackout around gold-critical events · venue-side SL always on

Run:
  python ripper.py                 # PAPER mode (default, safe)
  RIPPER_MODE=LIVE python ripper.py   # Bitget live (keys required)
  python ripper.py --status        # print rails snapshot
  python ripper.py --reset         # clear rails state (new day)

Linux: ./install_linux.sh  (systemd user service, Restart=always)
Windows: install_windows.ps1 (Task Scheduler, restart on failure)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backtest import _block_key, _day_key  # noqa: E402
from brokers.bitget_broker import BitgetBroker  # noqa: E402
from brokers.paper_broker import PaperBroker  # noqa: E402
from engine import commands as cmdchan  # noqa: E402
from engine import assurance  # noqa: E402
from engine import guards  # noqa: E402
from engine.config import CONFIG, Config  # noqa: E402
from engine.divergence import alert_text as drift_alert_text  # noqa: E402
from engine.divergence import check as divergence_check  # noqa: E402
from engine.fasttape import FastTape  # noqa: E402
from engine.divergence import record_history as drift_record  # noqa: E402
from engine.indicators import atr as atr_fn  # noqa: E402
from engine.indicators import session_of  # noqa: E402
from engine.journal import journal_trade, log_event  # noqa: E402
from engine.news_guard import NewsGuard  # noqa: E402
from engine.notify import fmt_snapshot, send_error_once, send_telegram  # noqa: E402
from engine.protections import ProtectionManager  # noqa: E402
from engine.reconcile import alert_text as recon_alert_text  # noqa: E402
from engine.reconcile import run as reconcile_run  # noqa: E402
from engine.risk import RiskRails  # noqa: E402
from engine.strategy import Position, StrategyEngine  # noqa: E402

MODE = {"PAPER": "PAPER", "LIVE": "LIVE"}


def banner(cfg: Config) -> None:
    print(r"""
 ██╗  ██╗ ██████╗ ██╗     ██████╗
 ██║  ██║██╔════╝ ██║    ██╔════╝
 ███████║██║  ███╗██║    ██║
 ██║  ██║██║   ██║██║    ██║
 ██║  ██║╚██████╔╝███████╗╚██████╗
 ╚═╝  ╚═╝ ╚═════╝ ╚══════╝ ╚═════╝""")
    print(f"  v{cfg.version} | mode={cfg.mode} | rails: "
          f"+${cfg.block_target_usd}/- ${cfg.block_max_loss_usd} per 4h · "
          f"+${cfg.day_target_usd}/-${cfg.day_max_loss_usd} per day")


class GoldRipper:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        cfg.apply_calibration()               # trained params override defaults
        self.engine = StrategyEngine(cfg)
        self.rails = RiskRails(cfg)
        self.news = NewsGuard(cfg)
        self.broker: PaperBroker | BitgetBroker = (
            BitgetBroker(cfg) if cfg.mode == "LIVE" else PaperBroker(cfg))
        self.running = True
        self.started_at = time.time()
        self.last_error: str | None = None
        self._price: float | None = None
        self._live_equity: float | None = None
        self._last_bar_ts = None
        self._h1 = None
        self._ctx = None
        self._pending = None          # armed maker limit awaiting fill
        self._pending_age = 0
        self._pending_placed_ts: float | None = None
        # drift self-check state (engine/divergence.py)
        self._drift_report: dict | None = None
        self._drift_status: str | None = None
        self._drift_last_check = 0.0
        self._drift_last_alert = 0.0
        # venue reconciliation state (engine/reconcile.py, LIVE mode)
        self._recon_report: dict | None = None
        self._recon_last_call = 0.0
        self._recon_alerted = False
        self._recon_busy = False
        # remote command channel (engine/commands.py) + pause state.
        # STICKY pause: pause.flag survives restarts - a paused bot that
        # crashed overnight must never wake up hunting on its own.
        self.paused = cmdchan.pause_flag_active(cfg)
        if self.paused:
            print("[!] sticky pause restored (data/pause.flag) - resume "
                  "via mission control or `rm data/pause.flag`")
        self.last_cmd: dict | None = None
        # bot-side retrain (command channel "retrain" action)
        self._train_lock = threading.Lock()
        self._train_req: dict | None = None   # who requested + when
        self._train_state: dict | None = None # live progress for the heartbeat
        # ── 9-repo audit merges ─────────────────────────────────────
        # protections framework (freqtrade merge): cooldown after every
        # close, stoploss guard, dollar drawdown guard - file-backed
        self.prot = ProtectionManager(cfg)
        # fast lane (fasttape merge): ms-class incremental indicators
        self.tape = FastTape()
        self._tape_last_write = 0.0
        self._swan: str = ""            # black-swan sentinel verdict
        # link circuit breaker (bitget-trading-bot merge)
        self._link_state = "OK"
        self._feed_fail_streak = 0
        # venue plan armor (bitget-trading-bot merge)
        self._plans_last_check = 0.0
        self._plans_just_placed = False
        # on-exchange SL ratchet throttle (freqtrade merge)
        self._sl_last_move = 0.0

    # ------------------------------------------------------------ lifecycle
    def start(self) -> int:
        banner(self.cfg)
        if self.cfg.mode == "LIVE":
            if not self.broker.connect():
                print("[!] LIVE connect failed - staying DOWN (never "
                      "silently paper). Fix .env or use RIPPER_MODE=PAPER.")
                return 3
        print(f"[+] engine RIPPER-4X online | broker={self.broker.name} | "
              f"trained params "
              f"{'loaded' if self.cfg.calibration_file.exists() else 'DEFAULTS'}")

        while self.running:
            try:
                self._poll_commands()
                self.tick()
                self.last_error = None
            except KeyboardInterrupt:
                break
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)[:200]
                log_event(self.cfg, "tick_error", {"error": str(e)})
                send_error_once("tick-error",
                                "<b>TICK ERROR</b> " + self.last_error,
                                cooldown_s=1800)
                time.sleep(10)
            self._selfcheck()
            self._reconcile()
            self._heartbeat()
            time.sleep(self.cfg.poll_seconds)
        self.broker.disconnect()
        print("[+] ripper offline. state saved.")
        return 0

    def _stop(self, *_a) -> None:
        self.running = False

    def _heartbeat(self) -> None:
        try:
            # rail-transition alerts (Telegram, optional)
            _rsnap = self.rails.snapshot()
            _rkey = (_rsnap["block_status"], _rsnap["day_status"])
            if _rkey != getattr(self, "_last_rail_key", None):
                if hasattr(self, "_last_rail_key"):
                    send_telegram(
                        f"<b>RAILS</b> block {_rsnap['block_status']} · day "
                        f"{_rsnap['day_status']}\nblock "
                        f"${_rsnap['block_pnl']:+.2f} · day "
                        f"${_rsnap['day_pnl']:+.2f}")
                self._last_rail_key = _rkey
            sym = (getattr(self.broker, "symbol", "")
                   or ("XAU" if self.cfg.mode == "PAPER" else ""))
            stress = None
            snap_fn = getattr(self.broker, "stress_snapshot", None)
            if callable(snap_fn):
                stress = snap_fn()
            snap = {"ts": time.time(),
                    "iso": datetime.now(timezone.utc).isoformat(),
                    "version": self.cfg.version,
                    "mode": self.cfg.mode,
                    "broker": self.broker.name,
                    "symbol": sym.replace(":USDT", "").replace("/", ""),
                    "started_at": self.started_at,
                    "uptime_s": round(time.time() - self.started_at),
                    "price": self._price,
                    "equity": (self._live_equity
                               if self.cfg.mode == "LIVE" else None),
                    "rails": self.rails.snapshot(),
                    "open": len(self.broker.open_positions()),
                    "pending": 1 if self._pending is not None else 0,
                    "news_next": self.news.next_event(),
                    "stress": stress,
                    "feed": getattr(self.broker, "feed_meta", None),
                    "drift": self._drift_report,
                    "reconcile": self._reconcile_hb(),
                    "prot": self.prot.active(),
                    "fast": (self.tape.json_safe()
                             if self.cfg.fast_lane else None),
                    "link": {"state": self._link_state,
                             "fails": self._feed_fail_streak},
                    "swan": self._swan,
                    "assure": getattr(self, "_assure_lvl", 0),
                    "paused": self.paused,
                    "sticky": cmdchan.pause_flag_active(self.cfg),
                    "train": self._train_heartbeat(),
                    "cmd": self.last_cmd,
                    "last_error": self.last_error}
            tmp = self.cfg.heartbeat_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(snap, default=str))
            tmp.replace(self.cfg.heartbeat_file)
        except Exception:  # noqa: BLE001
            pass

    def _selfcheck(self) -> None:
        """Periodic drift self-check: journal vs calibration. On a fresh
        DRIFTED verdict, push a Telegram alert (cooldown-guarded)."""
        if not self.cfg.self_check:
            return
        now = time.time()
        if now - self._drift_last_check < self.cfg.selfcheck_interval_s:
            return
        self._drift_last_check = now
        try:
            rep = divergence_check(self.cfg)
            prev = self._drift_status
            self._drift_status = rep.get("status")
            self._drift_report = rep
            if rep.get("status") == "DRIFTED" and prev != "DRIFTED" and \
                    now - self._drift_last_alert > \
                    self.cfg.drift_alert_cooldown_min * 60:
                self._drift_last_alert = now
                send_telegram(drift_alert_text(rep))
                log_event(self.cfg, "drift_alert",
                          {"status": rep.get("status"),
                           "n": rep.get("n"),
                           "tests": rep.get("tests")})
            drift_record(self.cfg, rep)
        except Exception:  # noqa: BLE001 - never kill the loop over a check
            pass

    # ------------------------------------------------------------ reconcile
    def _reconcile_hb(self) -> dict | None:
        """Reconcile report for the heartbeat, with a human age stamp."""
        rep = self._recon_report
        if not rep:
            return None
        out = dict(rep)
        out["age_min"] = round(max(0.0, time.time()
                                   - float(rep.get("ts") or 0)) / 60)
        return out

    def _reconcile(self) -> None:
        """LIVE: diff the journal against Bitget's own fill history every
        reconcile_min. Runs in a single-flight daemon thread with the same
        hard cap philosophy as the feed - a slow venue call can never stall
        the trading loop. A GAP (venue filled something the journal never
        recorded) pushes a Telegram alert, cooldown-guarded."""
        if self.cfg.mode != "LIVE" or self._recon_busy:
            return
        now = time.time()
        if now - self._recon_last_call < self.cfg.reconcile_min * 60:
            return
        self._recon_last_call = now

        def _job() -> None:
            try:
                rep = reconcile_run(self.cfg, self.broker)
                self._recon_report = rep
                if rep.get("status") == "GAP":
                    # alert once per GAP streak (re-armed by the next OK)
                    if not self._recon_alerted:
                        self._recon_alerted = True
                        send_telegram(recon_alert_text(rep))
                        log_event(self.cfg, "reconcile_gap",
                                  {"unmatched": rep.get("unmatched_n"),
                                   "fills": rep.get("venue_fills_n")})
                elif rep.get("status") == "OK":
                    self._recon_alerted = False  # re-arm for the next GAP
            except Exception as e:  # noqa: BLE001 - never kill the loop
                self._recon_report = {"status": "ERROR",
                                      "note": str(e)[:120], "ts": time.time()}
            finally:
                self._recon_busy = False

        threading.Thread(target=_job, daemon=True,
                         name="ripper-reconcile").start()

    # ------------------------------------------------------------ commands
    def _poll_commands(self) -> None:
        """Dashboard -> bot command file: pause / resume / close_all /
        cancel_pending. Executes, acks into commands_status, deletes the
        file so a command can never run twice."""
        cmd = cmdchan.read_pending(self.cfg)
        if cmd is None:
            return
        action = str(cmd.get("action", "")).lower()
        cid = cmd.get("id")
        try:
            detail = self._execute_command(action)
            cmdchan.write_status(self.cfg, {"action": action, "id": cid,
                                           "ok": True, "ts": time.time(),
                                           "detail": detail})
            self.last_cmd = {"action": action, "id": cid, "ok": True,
                             "ts": time.time(), "detail": detail}
            log_event(self.cfg, "command", {"action": action,
                                            "detail": detail})
        except Exception as e:  # noqa: BLE001
            cmdchan.write_status(self.cfg, {"action": action, "id": cid,
                                           "ok": False, "ts": time.time(),
                                           "detail": str(e)[:160]})
            self.last_cmd = {"action": action, "id": cid, "ok": False,
                             "ts": time.time(), "detail": str(e)[:160]}
        finally:
            cmdchan.drop(self.cfg, cmd)

    def _execute_command(self, action: str) -> str:
        if action == "pause":
            if not self.paused:
                self.paused = True
                cmdchan.set_pause_flag(self.cfg, "mission control pause")
                send_telegram("<b>PAUSED</b> (sticky) - ripper stands down: "
                              "no NEW entries (open positions still managed, "
                              "pause survives restarts)\n"
                              f"{fmt_snapshot(self.cfg)}")
                log_event(self.cfg, "paused", {"sticky": True})
            return "paused - no new entries (sticky flag written)"
        if action == "resume":
            if self.paused:
                self.paused = False
                cmdchan.clear_pause_flag(self.cfg)
                send_telegram("<b>RESUMED</b> - ripper is hunting again\n"
                              f"{fmt_snapshot(self.cfg)}")
                log_event(self.cfg, "resumed", {})
            return "resumed - hunting"
        if action == "close_all":
            closed, realized = self._flatten("MANUAL")
            self._cancel_pending_limit()
            send_telegram(f"<b>KILL SWITCH</b> - closed {closed} position(s), "
                          f"realized ${realized:+.2f}\n{fmt_snapshot(self.cfg)}")
            log_event(self.cfg, "close_all", {"closed": closed,
                                              "pnl": round(realized, 2)})
            return f"closed {closed} position(s), realized ${realized:+.2f}"
        if action == "cancel_pending":
            ok = self._cancel_pending_limit()
            return "pending limit canceled" if ok else "no pending limit"
        if action == "retrain":
            return self._start_retrain()
        if action == "feed_check":
            return self._feed_check()
        if action == "reconcile_now":
            if self.cfg.mode != "LIVE":
                return ("reconcile is LIVE-only - paper has no venue fill "
                        "history to diff against")
            # force the very next loop tick to run a reconciliation cycle
            # (single-flight thread, same as the timer path - never on the
            # loop thread); the fresh report rides the heartbeat "reconcile"
            self._recon_last_call = 0.0
            log_event(self.cfg, "reconcile_now", {"forced_by": "operator"})
            return ("reconcile forced - fresh report rides the heartbeat "
                    "within one loop cycle")
        raise ValueError(f"unknown action {action}")

    def _feed_check(self) -> str:
        """Force a fresh tape pull and ack exactly which layer answered."""
        info = self.broker.refresh_feed("1h")
        if info.get("ok"):
            detail = (f"feed OK · {info.get('source')} · "
                      f"{info.get('bars')} bars · {info.get('latency_s')}s · "
                      f"px {info.get('px')}")
        else:
            detail = (f"feed DEGRADED · {info.get('source')} · "
                      f"{info.get('bars')} bars · "
                      f"{info.get('latency_s')}s · "
                      f"{info.get('note', 'no fresh tape')}")
        log_event(self.cfg, "feed_check", info)
        return detail

    # ------------------------------------------------------------ bot-side retrain
    # Built-in tape map for the fetch step; extend per deployment with the
    # RIPPER_YF_MAP env var (JSON, e.g. {"BTC": "BTC-USD"}).
    _YF_MAP = {"XAU": "GC=F", "GOLD": "GC=F", "XAG": "SI=F",
               "SILVER": "SI=F"}

    def _yf_symbol(self) -> str:
        try:
            extra = json.loads(os.getenv("RIPPER_YF_MAP", "{}"))
            if isinstance(extra, dict):
                self._YF_MAP.update({str(k).upper(): str(v)
                                     for k, v in extra.items()})
        except Exception:  # noqa: BLE001
            pass
        tag = self.cfg.symbol_tag or "XAU"
        return self._YF_MAP.get(tag, self._YF_MAP.get("XAU", "GC=F"))

    def _start_retrain(self) -> str:
        """Run fetch_history + train.py in THIS interpreter/venv, in a
        background thread. Progress rides the heartbeat 'train' field."""
        if not self._train_lock.acquire(blocking=False):
            return "retrain already running"
        self._train_req = {"iso": cmdchan.datetime_iso(),
                           "pid": os.getpid()}
        threading.Thread(target=self._run_retrain, daemon=True,
                         name="ripper-retrain").start()
        return "retrain started (fetch + walk-forward train)"

    def _run_retrain(self) -> None:
        tag = self.cfg.symbol_tag.lower()
        yf = self._yf_symbol()
        fetch = [sys.executable, "data/fetch_history.py"]
        train = [sys.executable, "train.py"]
        if tag:
            fetch += ["--tag", tag, "--yf-symbol", yf]
            train += ["--tag", tag]
        try:
            self._train_state = {"state": "running",
                                 "step": f"fetching {tag or 'gold'} history ({yf})",
                                 "requested": self._train_req,
                                 "started": cmdchan.datetime_iso()}
            self._write_train_status()
            r = subprocess.run(fetch, cwd=ROOT, capture_output=True, text=True,
                               timeout=900)
            if r.returncode != 0:
                raise RuntimeError(
                    f"fetch_history exited {r.returncode}: "
                    f"{(r.stderr or r.stdout)[-160:]}")
            self._train_state = {**self._train_state,
                                 "step": "walk-forward training (~2 min)"}
            self._write_train_status()
            r = subprocess.run(train, cwd=ROOT, capture_output=True, text=True,
                               timeout=1800)
            if r.returncode != 0:
                raise RuntimeError(
                    f"train.py exited {r.returncode}: "
                    f"{(r.stderr or r.stdout)[-160:]}")
            self._train_state = {"state": "done",
                                 "step": "calibration updated",
                                 "at": cmdchan.datetime_iso()}
            self._write_train_status()
            send_telegram("<b>RETRAIN</b> - calibration rebuilt inside the "
                          f"bot ({tag or 'XAU'} · {yf})\n"
                          f"{fmt_snapshot(self.cfg)}")
            log_event(self.cfg, "retrain_done", {"tag": tag or "XAU"})
            # hot-reload the new params without a restart: the engine and
            # broker hold a reference to this same Config object, so
            # apply_calibration() mutates the params they read live.
            self.cfg.apply_calibration()
        except Exception as e:  # noqa: BLE001 - status, never kill the loop
            self._train_state = {"state": "error",
                                 "error": str(e)[:200],
                                 "at": cmdchan.datetime_iso()}
            self._write_train_status()
            log_event(self.cfg, "retrain_error", {"error": str(e)[:160]})
        finally:
            self._train_lock.release()

    def _write_train_status(self) -> None:
        try:
            p = self.cfg.train_status_file
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._train_state, default=str))
            tmp.replace(p)
        except Exception:  # noqa: BLE001
            pass

    def _train_heartbeat(self) -> dict | None:
        """Train status for the heartbeat: live progress if this process is
        training, else the last known status file (persists across restarts)."""
        if self._train_state is not None:
            return self._train_state
        try:
            return json.loads(self.cfg.train_status_file.read_text())
        except Exception:  # noqa: BLE001
            return None

    def _cancel_pending_limit(self) -> bool:
        p = self._pending
        if p is None:
            return False
        if p.get("oid"):
            try:
                self.broker.cancel_limit(p["oid"])
            except Exception:  # noqa: BLE001
                pass
        self._pending = None
        log_event(self.cfg, "pending_canceled", {"side": p.get("side")})
        return True

    def _flatten(self, reason: str = "MANUAL") -> tuple[int, float]:
        """Market-close every open position at the last price. Paper: fills
        at the live tape (stress model applies - a kill is still a taker
        fill). LIVE: venue market-close per position."""
        closed_n = 0
        realized = 0.0
        for p in self.broker.open_positions():
            try:
                if isinstance(self.broker, PaperBroker):
                    px = self.broker.last_price() or float(p["entry"])
                    pnl, closed = self.broker.close_position(
                        p["ticket"], px, 1.0, reason)
                    if closed:
                        self.rails.register_close(pnl, True)
                        self._journal_close(p, px, pnl)
                        closed_n += 1
                        realized += pnl
                else:
                    res = self.broker.close_position(p["side"],
                                                     float(p["size"]))
                    if res.ok:
                        closed_n += 1
                        self.rails.register_close(0.0, is_full_close=True)
            except Exception as e:  # noqa: BLE001 - keep flattening the rest
                log_event(self.cfg, "flatten_error",
                          {"ticket": p.get("ticket"), "error": str(e)})
        return closed_n, realized

    # ------------------------------------------------------------ feed
    def _refresh_candles(self):
        df = self.broker.candles("1h", 400)
        if df is None or len(df) < 260:
            self._feed_fail_streak += 1
            self._link_watch(False)
            return False
        self._h1 = df
        self._ctx = self.engine._context(df)
        self._feed_fail_streak = 0
        self._link_watch(True)
        self._tape_feed(df)
        return True

    # ------------------------------------------------------------ link watch
    def _link_watch(self, ok: bool) -> None:
        """Internet-down circuit breaker (bitget-trading-bot merge): N
        consecutive feed failures = ONE alert, recovery = ONE alert. An
        outage must never become alert spam - and the operator must know
        the venue-side stops are still live while the tape is dark."""
        if ok:
            if self._link_state != "OK":
                self._link_state = "OK"
                send_error_once("link-restored",
                                "<b>LINK RESTORED</b> - tape is flowing "
                                "again", cooldown_s=60)
            return
        if self._feed_fail_streak >= 3 and self._link_state == "OK":
            self._link_state = "DOWN"
            send_error_once("link-down",
                            "<b>LINK DOWN</b> - "
                            f"{self._feed_fail_streak} consecutive feed "
                            "failures. Venue-side SL/TP stay live; the bot "
                            "will keep retrying.", cooldown_s=1800)

    # ------------------------------------------------------------ fast lane
    def _tape_feed(self, df) -> None:
        """Feed CLOSED bars into the ms-class tape (the forming bar never
        touches incremental state - numbers stay identical to pandas)."""
        if not self.cfg.fast_lane:
            return
        try:
            cutoff = datetime.now(timezone.utc).replace(
                minute=0, second=0, microsecond=0)
            closed = df[df.index < cutoff]
            if self.tape.n_push == 0:
                self.tape.warmup(closed)
            else:
                last = self.tape.last_ts
                new = closed[closed.index > last] if last is not None else closed
                for ts, row in new.iterrows():
                    self.tape.push(float(row["open"]), float(row["high"]),
                                   float(row["low"]), float(row["close"]),
                                   ts=ts)
        except Exception:  # noqa: BLE001 - the tape never kills the loop
            pass

    def _fast_lane(self) -> None:
        """Per-tick fast duties: indicator snapshot in MICROSECONDS, tape
        write for the dashboard (data/tape.json), black-swan sentinel."""
        if not self.cfg.fast_lane or not self.tape.n_push:
            return
        try:
            snap = self.tape.json_safe()
            now = time.time()
            if now - self._tape_last_write >= self.cfg.fast_write_s:
                self._tape_last_write = now
                p = self.cfg.tape_file
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".tmp")
                tmp.write_text(json.dumps(snap, default=str))
                tmp.replace(p)
            df = self._h1
            a = float(snap.get("atr") or 0)
            if df is not None and len(df) > 2 and a > 0:
                swan, swhy = guards.black_swan(df, len(df) - 1, a)
                self._swan = swhy if swan else ""
                if swan:
                    log_event(self.cfg, "black_swan", {"why": swhy})
                    send_error_once("swan",
                                    "<b>BLACK SWAN</b> - new entries "
                                    f"suspended: {swhy}. Stops stay live.",
                                    cooldown_s=1800)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ plan armor
    def _plans_armor(self) -> None:
        """Venue plan-order armor (bitget-trading-bot merge, LIVE): an
        open position must always carry full-position TP/SL plan orders;
        a missing leg is re-placed so a crashed bot can never leave a
        naked position. Race-guarded (a fresh plan may not appear in the
        pending list for a beat)."""
        if self.cfg.mode != "LIVE" or not self.cfg.plans_armor:
            return
        if self._plans_just_placed:
            self._plans_just_placed = False   # one-cycle race guard
            return
        now = time.time()
        if now - self._plans_last_check < self.cfg.plans_check_s:
            return
        self._plans_last_check = now
        try:
            poss = self.broker.open_positions()
            if not poss:
                return
            p = poss[0]
            sl = float(p.get("sl") or 0)
            tp1 = float(p.get("tp1") or 0)
            if sl <= 0 or tp1 <= 0:
                return
            kinds = {pl.get("kind") for pl in self.broker.pending_plans()}
            missing = {"pos_loss", "pos_profit"} - kinds
            if missing:
                if self.broker.place_pos_plans(p["side"], float(p["size"]),
                                               sl, tp1, legs=missing):
                    self._plans_just_placed = True
                    log_event(self.cfg, "plans_reattached",
                              {"missing": sorted(missing)})
        except Exception:  # noqa: BLE001 - armor never kills the loop
            pass

    # ------------------------------------------------------------ the loop body
    def tick(self) -> None:
        cfg = self.cfg
        if self._refresh_candles() is False:
            print("[!] feed not ready - retrying")
            return
        df = self._h1
        ts_now = datetime.now(timezone.utc)
        px = float(df["close"].iloc[-1])
        self._price = px
        if isinstance(self.broker, PaperBroker):
            self.broker.set_price(px)

        # live equity sync (LIVE only)
        if cfg.mode == "LIVE":
            eq = self.broker.equity()
            if eq:
                self._live_equity = eq
                self.rails.sync_equity(eq)

        self._manage_open(df)
        # fast lane duties (ms-class indicators + black-swan sentinel) and
        # LIVE venue plan armor - both never stall the trading loop
        self._fast_lane()
        self._plans_armor()

        # PAUSED: exits + rails stay live, but no new entries and the
        # pending limit keeps working (it was placed before the pause) -
        # operators who want it gone use cancel_pending / close_all.
        if self.paused:
            return

        # rails gate
        allowed, why = self.rails.can_enter()
        if not allowed:
            return

        # pending maker limit: check venue/paper fill / expiry first
        if self._pending is not None:
            self._pending_age += 1
            sig_p = self._pending
            expired = (
                self._pending_placed_ts is not None
                and time.time() - self._pending_placed_ts
                > self.cfg.maker_wait_bars * 3600)
            if sig_p.get("oid"):
                # LIVE: poll the actual venue order
                state, fill_px = self.broker.limit_status(sig_p["oid"])
                if state == "filled":
                    self.rails.register_attempt()
                    print(f"[>>>] KILL (maker) {sig_p['side']} "
                          f"{sig_p['units']} @ {fill_px or sig_p['px']:.2f} | "
                          f"{sig_p['module']} | venue limit filled")
                    send_telegram(
                        f"<b>ENTRY</b> (maker) {sig_p['side']} "
                        f"{sig_p['units']} XAU @ {(fill_px or sig_p['px']):.2f}\n"
                        f"{sig_p['module']} · risk ${sig_p['risk']:.2f}\n"
                        f"{fmt_snapshot(cfg)}")
                    self._pending = None
                elif state in ("canceled", "rejected"):
                    self._pending = None
                elif expired:
                    self.broker.cancel_limit(sig_p["oid"])
                    print(f"[~] venue limit expired after "
                          f"{self.cfg.maker_wait_bars} bars - canceled")
                    self._pending = None
                else:
                    return  # still working on the book
            else:
                # PAPER: touch-fill simulation at the limit price
                filled = (px <= sig_p["px"]) if sig_p["side"] == "LONG" \
                    else (px >= sig_p["px"])
                if filled:
                    units, risk = self.rails.size_for(sig_p["px"], sig_p["sl"])
                    if units > 0:
                        res = self.broker.market_order(
                            sig_p["side"], units, sig_p["sl"], sig_p["tp1"],
                            sig_p["module"],
                            f"{sig_p['reason']} [maker-limit fill]",
                            fill_px=sig_p["px"])
                        if res.ok:
                            self.rails.register_attempt()
                            print(f"[>>>] KILL {sig_p['side']} {units} @ "
                                  f"{sig_p['px']:.2f} | {sig_p['module']} | "
                                  f"risk ${risk:.2f}")
                            send_telegram(
                                f"<b>ENTRY</b> {sig_p['side']} {units} XAU @ "
                                f"{sig_p['px']:.2f}\n{sig_p['module']} · risk "
                                f"${risk:.2f}\n{fmt_snapshot(cfg)}")
                    self._pending = None
                elif expired:
                    print(f"[~] limit unfilled after "
                          f"{self.cfg.maker_wait_bars} bars - expired")
                    self._pending = None
                else:
                    return  # keep waiting for the touch

        # news gate
        blocked, nwhy = self.news.blocked(ts_now)
        if blocked:
            log_event(cfg, "news_block", {"why": nwhy})
            return

        # black-swan sentinel (astra-quant merge): a quake bar suspends
        # NEW entries - exits, stops and pending fills all stay live
        if self._swan:
            return

        # closed-bar check: only act on a NEW closed H1 bar
        last_closed = df.index[-2] if df.index[-1] > ts_now.replace(
            minute=0, second=0, microsecond=0) else df.index[-1]
        if self._last_bar_ts == last_closed:
            return
        self._last_bar_ts = last_closed

        sess = session_of(ts_now)
        i = len(df) - 1
        # daily assurance (zero-trade-day killer): late in the day with
        # no fill yet, escalate gate relaxation L1..L3 - rails stay hard
        lvl = assurance.level(ts_now, self.rails.state.day_attempts,
                              self._pending is not None, cfg)
        if lvl > 0 and lvl != getattr(self, "_assure_lvl", 0):
            log_event(cfg, "assure_engaged",
                      {"level": lvl,
                       "day_attempts": self.rails.state.day_attempts})
            if lvl == 1:
                send_telegram(
                    "&#9881;&#65039; <b>DAILY ASSURE L1</b> - no fill yet "
                    "today, entry gates relaxing (rails untouched)")
        self._assure_lvl = lvl
        sig = self.engine.evaluate(df, self._ctx, i, ts_now, assure=lvl)
        if sig is None:
            return

        # protections gate (freqtrade merge): locks are side-aware and
        # never negotiated - a lock reason is logged, not fought
        pok, pwhy = self.prot.can_enter(sig.side)
        if not pok:
            log_event(cfg, "protection_block",
                      {"why": pwhy, "module": sig.module, "side": sig.side})
            return
        # pre-entry spread guard (freqtrade SpreadFilter merge, LIVE only:
        # a wide book is a news/rollover tell)
        if cfg.mode == "LIVE":
            sp = self.broker.spread_bps()
            if sp is not None and sp > guards.MAX_SPREAD_BPS:
                log_event(cfg, "spread_block", {"bps": round(sp, 2),
                                                "module": sig.module})
                return
        # paper pending arming: price-cross sanity (astra-quant merge -
        # the LIVE path enforces the same guard inside limit_order)
        if cfg.maker_entry and cfg.mode != "LIVE":
            pcok, pcwhy = guards.price_cross_ok(sig.side, float(sig.entry), px)
            if not pcok:
                log_event(cfg, "price_cross_refused", {"why": pcwhy})
                return

        if cfg.maker_entry:
            # post-only limit at the signal close - fills on the touch,
            # expires after maker_wait_bars (mirrors the calibration).
            # LIVE: a REAL post-only order on the venue (maker 0.02% fee)
            # with attached SL/TP; paper: touch-fill simulation.
            if cfg.mode == "LIVE":
                units, risk = self.rails.size_for(sig.entry, sig.sl)
                if units <= 0:
                    return
                res = self.broker.limit_order(
                    sig.side, units, float(sig.entry), float(sig.sl),
                    float(sig.tp1), sig.module, sig.reason)
                if res.ok:
                    self._pending = {"side": sig.side,
                                     "px": float(sig.entry),
                                     "sl": float(sig.sl),
                                     "tp1": float(sig.tp1),
                                     "module": sig.module,
                                     "reason": sig.reason,
                                     "oid": res.ticket,
                                     "units": units, "risk": risk}
                    self._pending_age = 0
                    self._pending_placed_ts = time.time()
                    print(f"[>] venue limit placed {sig.side} {units} @ "
                          f"{sig.entry:.2f} ({sig.module}) oid={res.ticket}")
                    return
                # post-only rejected (crossing) or venue error -> take the
                # taker path now; a missed trade is worse than 0.04% fee.
                print(f"[!] limit rejected ({res.error}) - falling back "
                      f"to taker market entry")
            else:
                self._pending = {"side": sig.side, "px": float(sig.entry),
                                 "sl": float(sig.sl), "tp1": float(sig.tp1),
                                 "module": sig.module, "reason": sig.reason}
                self._pending_age = 0
                self._pending_placed_ts = time.time()
                print(f"[>] limit armed {sig.side} @ {sig.entry:.2f} "
                      f"({sig.module})")
                return
        units, risk = self.rails.size_for(sig.entry, sig.sl)
        if units <= 0:
            return
        res = self.broker.market_order(sig.side, units, sig.sl, sig.tp1,
                                       sig.module, sig.reason)
        if res.ok:
            self.rails.register_attempt()
            print(f"[>>>] KILL {sig.side} {units} @ {res.price or sig.entry:.2f}"
                  f" | {sig.module} | {sig.reason} | risk ${risk:.2f}")
            log_event(cfg, "entry", {"side": sig.side, "units": units,
                                     "module": sig.module, "risk": risk,
                                     "reason": sig.reason})
            send_telegram(
                f"<b>ENTRY</b> {sig.side} {units} XAU @ "
                f"{(res.price or sig.entry):.2f}\n{sig.module} · risk "
                f"${risk:.2f}\n{fmt_snapshot(cfg)}")
        else:
            print(f"[!] order rejected: {res.error}")

    # ------------------------------------------------------------ exits
    def _manage_open(self, df) -> None:
        cfg = self.cfg
        open_pos = self.broker.open_positions()
        if not open_pos:
            return
        a = float(atr_fn(df, 14).iloc[-1])
        live = cfg.mode == "LIVE"
        for p in open_pos:
            # how long has this trade REALLY been open? The paper broker
            # stamps opened_ts; live venue dicts may expose a timestamp.
            # opened_bar=0 would make the ladder's time stop fire on the
            # very first tick (i - 0 >= time_stop_bars) and gut every
            # trade at market - so unknown age is treated as BRAND NEW
            # (time stop counts from first sight instead).
            bars_held = 0
            ots = p.get("opened_ts")
            if not ots:
                try:
                    vt = p.get("timestamp")  # ccxt fetch_positions, ms
                    if vt:
                        ots = datetime.fromtimestamp(
                            float(vt) / 1000, tz=timezone.utc).isoformat()
                except Exception:  # noqa: BLE001
                    ots = None
            if ots:
                try:
                    t0 = datetime.fromisoformat(
                        str(ots).replace("Z", "+00:00"))
                    bars_held = max(0, int(
                        (datetime.now(timezone.utc) - t0).total_seconds()
                        // 3600))
                except Exception:  # noqa: BLE001
                    bars_held = 0
            # per-module exit params (the ladder must match how the module
            # was calibrated - M2 meanrev exits in hours, M1 trend in days)
            mod = str(p.get("module", "")).upper()
            trail, tstop = {
                "M1_TREND": (cfg.m1_trail_atr, cfg.m1_time_stop_bars),
                "M2_MEANREV": (cfg.m2_trail_atr, cfg.m2_time_stop_bars),
                "M3_SWEEP": (cfg.m3_trail_atr, cfg.m3_time_stop_bars),
                "M4_NYBREAK": (cfg.m4_trail_atr, cfg.m4_time_stop_bars),
            }.get(mod, (cfg.m1_trail_atr, cfg.m1_time_stop_bars))
            # build a Position-shaped view for the shared exit ladder
            pos = Position(
                side=p["side"], entry=float(p["entry"]), sl=float(p["sl"]),
                tp1=float(p["tp1"]), size=float(p["size"]),
                module=p.get("module", ""), opened_ts=ots,
                opened_bar=len(df) - 1 - bars_held,
                tp1_done=bool(p.get("tp1_done")),
                be_moved=bool(p.get("be_moved")),
                orig_sl=float(p.get("orig_sl") or p["sl"]),
                trail_atr_mult=trail,
                time_stop_bars=tstop)
            # venue-side SL/TP handles fills; software ladder adjusts
            action, price, frac = self.engine.manage(
                pos, df, len(df) - 1, a)
            # NOTE: on live we mirror ladder moves to the venue via set_stop;
            # on paper we execute fills directly.
            if live:
                if action in ("BE", "TRAIL") and price:
                    # on-exchange SL ratchet (freqtrade merge): only move
                    # the stop if it IMPROVES, throttled to one venue call
                    # per minute - a flapping trigger is worse than a late one
                    cur = float(p.get("sl") or 0.0)
                    improves = cur == 0.0 or \
                        (p["side"] == "LONG" and price > cur) or \
                        (p["side"] == "SHORT" and price < cur)
                    if improves and time.time() - self._sl_last_move >= 60:
                        if self.broker.set_stop(p["size"], price, p["side"]):
                            self._sl_last_move = time.time()
                if action == "TP1" and not pos.tp1_done:
                    res = self.broker.close_position(
                        p["side"], p["size"] * cfg.tp1_fraction)
                    if res.ok:
                        self.rails.register_close(
                            0.0, is_full_close=False)
            else:
                ticket = p["ticket"]
                if action == "TP1" and not pos.tp1_done:
                    pnl, closed = self.broker.close_position(
                        ticket, price or pos.tp1, cfg.tp1_fraction, "TP1")
                    self.broker.mark_tp1(ticket)
                    if closed:
                        self.rails.register_close(pnl, True)
                        self._journal_close(p, price or pos.tp1, pnl)
                    else:
                        self.rails.register_close(pnl, False)
                elif action in ("EXIT_SL", "TIME", "EXIT_TP"):
                    pnl, closed = self.broker.close_position(
                        ticket, price or pos.sl, 1.0, action)
                    if closed:
                        self.rails.register_close(pnl, True)
                        self._journal_close(p, price or pos.sl, pnl)
                elif action in ("BE", "TRAIL") and price:
                    self.broker.modify_sl(ticket, price)
                # note: paper SL hits are also detected on the venue-side
                # simulation inside close_position calls above; hard SL
                # breach between polls is caught here:
                sl_hit = (p["side"] == "LONG" and float(df["low"].iloc[-1])
                          <= float(p["sl"])) or \
                         (p["side"] == "SHORT" and float(df["high"].iloc[-1])
                          >= float(p["sl"]))
                if sl_hit and self.broker.last_price() and \
                        p["ticket"] in [q["ticket"] for q in
                                        self.broker.open_positions()]:
                    pnl, closed = self.broker.close_position(
                        ticket, float(p["sl"]), 1.0, "SL-X")
                    if closed:
                        self.rails.register_close(pnl, True)
                        self._journal_close(p, float(p["sl"]), pnl)

    def _journal_close(self, p, exit_px, pnl) -> None:
        hold_s = ""
        try:
            ots = p.get("opened_ts")
            if ots:
                t0 = datetime.fromisoformat(str(ots).replace("Z", "+00:00"))
                hold_s = round((datetime.now(timezone.utc) - t0).total_seconds())
        except Exception:  # noqa: BLE001
            pass
        meta = getattr(self.broker, "last_close_meta", {}) or {}
        journal_trade(self.cfg, {
            "ts_open": p.get("opened_ts"),
            "ts_close": datetime.now(timezone.utc).isoformat(),
            "mode": self.cfg.mode, "symbol": getattr(self.broker, "symbol", ""),
            "side": p["side"], "module": p.get("module", ""),
            "size": p["size"], "entry": p["entry"], "exit": exit_px,
            "sl": p["sl"], "tp1": p["tp1"], "reason": p.get("reason", ""),
            "pnl": round(pnl, 2),
            "balance": round(self.rails.state.balance, 2),
            "hold_s": hold_s,
            "slip_bps": meta.get("slip_bps", ""),
            "spike": meta.get("spike", "")})
        # a fresh close can flip the drift verdict - recheck next heartbeat
        self._drift_last_check = 0.0
        # feed the protection guards (freqtrade merge): every journaled
        # full close arms the cooldown and re-scores the loss/drawdown
        # windows - a restart resumes this memory from the journal
        try:
            self.prot.on_exit(time.time(), float(pnl),
                              str(p.get("reason", "")),
                              str(p.get("side", "")), True)
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="GOLD RIPPER")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    cfg = CONFIG
    if args.reset:
        if cfg.state_file.exists():
            cfg.state_file.unlink()
        print("[+] rails state cleared")
        return 0
    if args.status:
        rails = RiskRails(cfg)
        print(json.dumps(rails.snapshot(), indent=2))
        return 0
    rip = GoldRipper(cfg)
    return rip.start()


if __name__ == "__main__":
    raise SystemExit(main())
