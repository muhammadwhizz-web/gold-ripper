"""
GOLD RIPPER :: Paper Broker
===========================
Realistic simulated fills on real fetched/live gold bars:
  - spread + slippage both sides (rates from cfg, Bitget-like)
  - STRESS MODEL (on by default): half-spread crossing, random spread
    spikes (news/illiquidity bursts) and latency slippage — so paper
    results never flatter the strategy
  - taker fees on every fill
  - equity/balance tracked, state persisted
This is the default mode. LIVE only after BITGET keys + RIPPER_MODE=LIVE.
"""
from __future__ import annotations

import json
import random
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

import pandas as pd

from brokers.base import OrderResult
from engine.config import Config
from engine.journal import log_event

STATE_FILE_POSTFIX = "_paper.json"
VALID_FEEDS = ("auto", "venue", "yahoo", "csv")


class PaperBroker:
    name = "PAPER"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.symbol = "XAUUSD-paper"
        self.positions: dict[str, dict] = {}
        self.balance: float = cfg.starting_balance
        self._last_px: float | None = None
        # rolling fill-realism window (surfaced on the dashboard heartbeat)
        self.recent_stress: deque = deque(maxlen=50)
        self.last_close_meta: dict = {}
        # feed hardening: a venue can hang (yfinance retries 429s FOREVER,
        # sockets stall). The loop must never wait on a tape.
        self._feed_lock = threading.Lock()
        self._feed_fetching = False
        self._last_feed: pd.DataFrame | None = None
        self._last_feed_ts = 0.0
        self._last_feed_source = ""
        # optional Bitget venue tape (public OHLCV, no keys needed)
        self._venue = None
        self._venue_busy = threading.Event()
        # what the dashboard heartbeat shows: which tape is live right now
        self.feed_meta: dict = {"source": "none", "bars": 0}
        self._load()

    # ------------------------------------------------------------ state
    def _state_path(self):
        return self.cfg.state_file.with_name(
            self.cfg.state_file.stem + STATE_FILE_POSTFIX)

    def _load(self) -> None:
        try:
            raw = json.loads(self._state_path().read_text())
            self.balance = float(raw.get("balance", self.cfg.starting_balance))
            self.positions = raw.get("positions", {})
        except Exception:  # noqa: BLE001
            pass

    def _save(self) -> None:
        try:
            self._state_path().parent.mkdir(parents=True, exist_ok=True)
            self._state_path().write_text(json.dumps({
                "balance": self.balance, "positions": self.positions}, indent=2))
        except OSError:
            pass

    # ------------------------------------------------------------ feed
    def set_price(self, px: float) -> None:
        self._last_px = px

    def last_price(self) -> float | None:
        return self._last_px

    def _venue_feed(self):
        """Lazily build the public Bitget tape; after a failure it lies low
        for 5 minutes so a blocked venue never costs a retry every poll."""
        if self.cfg.feed_source not in ("venue", "auto"):
            return None
        if self._venue is None:
            try:
                from engine.venue_feed import VenueFeed
                self._venue = VenueFeed(self.cfg)
            except Exception:  # noqa: BLE001
                return None
        return self._venue

    def _mark_feed(self, source: str, df: pd.DataFrame,
                   degraded: bool = False) -> None:
        self.feed_meta = {
            "source": source,
            "bars": int(len(df)),
            "age_s": round(max(0.0, time.time() - self._last_feed_ts), 1),
            "last_bar": str(df.index[-1]) if len(df) else "",
            "px": round(float(df["close"].iloc[-1]), 2) if len(df) else None,
            "degraded": bool(degraded),
        }

    def _yahoo(self, timeframe: str) -> pd.DataFrame:
        """Legacy Yahoo tape with the full hardening kit (timeout thread,
        single-flight, late-success lands in the cache)."""
        interval = "1h" if timeframe == "1h" else timeframe
        got: list = []
        if self._feed_lock.acquire(blocking=False):

            def _dl() -> None:
                try:
                    import yfinance as yf
                    period = "60d" if interval == "15m" else "730d"
                    df = yf.download("GC=F", period=period, interval=interval,
                                     progress=False, auto_adjust=True)
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = [c[0] for c in df.columns]
                    df = df.rename(columns=str.lower)[
                        ["open", "high", "low", "close", "volume"]].dropna()
                    df.index = pd.to_datetime(df.index, utc=True)
                    if df is not None and len(df):
                        got.append(df)   # a LATE success still counts
                        self._last_yahoo = df  # yahoo warm-start
                except Exception:  # noqa: BLE001 - fallback handles it
                    pass
                finally:
                    self._feed_fetching = False
                    try:
                        self._feed_lock.release()
                    except Exception:  # noqa: BLE001
                        pass

            self._feed_fetching = True
            th = threading.Thread(target=_dl, daemon=True,
                                  name="ripper-feed")
            th.start()
            th.join(self.cfg.feed_timeout_s)
        if got:
            return got[0]
        return getattr(self, "_last_yahoo", None) or pd.DataFrame()

    def _venue_df(self, timeframe: str, limit: int,
                  force: bool = False) -> pd.DataFrame | None:
        """Venue leg with a FULL hard cap: ccxt's load_markets() and the
        OHLCV pull BOTH run inside a daemon thread joined with
        RIPPER_FEED_TIMEOUT_S - a hung connect can never stall the loop
        (load_markets has no timeout of its own and HAS hung here).
        Single-flight: one venue thread at a time, a late success still
        lands in the venue cache for the next poll."""
        vf = self._venue_feed()
        if vf is None or self._venue_busy.is_set():
            return None
        got: list = []
        done = threading.Event()
        self._venue_busy.set()

        def _dl() -> None:
            try:
                if not vf.connect():
                    return
                df = (vf.refresh_now(timeframe, limit) if force
                      else vf.candles(timeframe, limit))
                if df is not None and len(df):
                    got.append(df)
            except Exception:  # noqa: BLE001 - caller falls back
                pass
            finally:
                self._venue_busy.clear()
                done.set()

        threading.Thread(target=_dl, daemon=True,
                         name="ripper-venue").start()
        # hard cap covers connect + fetch; on timeout the thread keeps
        # running and a late success still lands in the venue cache
        done.wait(self.cfg.feed_timeout_s)
        return got[0] if got else None

    def _fetch(self, timeframe: str, limit: int,
               force: bool = False) -> tuple[pd.DataFrame, str]:
        """Tape chain: venue -> yahoo (per RIPPER_FEED). Returns (df, src).
        force=True busts the venue's internal cache too (feed_check)."""
        # 1) Bitget public OHLCV - the exact perp LIVE mode trades
        if self.cfg.feed_source in ("venue", "auto"):
            df = self._venue_df(timeframe, self.cfg.venue_ohlcv_limit or limit,
                                force)
            if df is not None and len(df):
                src = getattr(self._venue, "symbol", "") or "venue"
                return df, f"bitget {src}"
        # 2) Yahoo GC=F futures (hardened)
        if self.cfg.feed_source in ("yahoo", "auto"):
            df = self._yahoo(timeframe)
            if df is not None and len(df):
                return df, "yahoo GC=F"
        return pd.DataFrame(), ""

    # ------------------------------------------------------------ stress
    def _stress(self, side: str, maker: bool) -> tuple[float, dict]:
        """Microstructure stress: returns (extra_cost_fraction, meta).

        - half-spread crossing (taker entries/exits buy the ask / hit the bid)
        - random spread spikes: news bursts and thin books widen the spread
          3-6x for a few percent of orders — the tape punishes exact-fill
          assumptions
        - latency slippage: 0..slip_bps drift against the trader while the
          order is in flight
        Disable with PAPER_STRESS=false (NOT recommended: it only makes the
        paper PnL prettier, never the live one).
        """
        if not self.cfg.paper_stress:
            return 0.0, {"stress": "off"}
        half = self.cfg.paper_spread_bps / 10_000.0
        spiked = random.random() < self.cfg.paper_spike_prob
        if spiked:
            half *= random.uniform(self.cfg.paper_spike_min_x,
                                   self.cfg.paper_spike_max_x)
        slip = random.uniform(0.0, self.cfg.paper_slip_bps) / 10_000.0
        # a resting post-only limit does NOT cross the spread, it earns the
        # spread - but latency slip still applies while the fill is in flight
        extra = 0.0 if maker else half
        extra += slip
        meta = {"stress": "on",
                "half_spread_bps": round(half * 10_000, 2),
                "spike": spiked,
                "slip_bps": round(slip * 10_000, 2)}
        self.recent_stress.append({k: meta[k] for k in
                                   ("half_spread_bps", "spike", "slip_bps")})
        return extra, meta

    def stress_snapshot(self) -> dict | None:
        """Rolling fill-realism stats for the heartbeat (last 50 fills).
        `recent` carries compact per-fill tuples [half_spread_bps, slip_bps,
        spike(0|1)] so the dashboard can draw a sparkline of paid spreads."""
        if not self.cfg.paper_stress:
            return None
        n = len(self.recent_stress)
        if n == 0:
            return {"fills": 0}
        spikes = sum(1 for s in self.recent_stress if s.get("spike"))
        avg_spread = sum(s.get("half_spread_bps", 0.0)
                         for s in self.recent_stress) / n
        avg_slip = sum(s.get("slip_bps", 0.0) for s in self.recent_stress) / n
        return {"fills": n,
                "avg_half_spread_bps": round(avg_spread, 2),
                "spike_rate": round(spikes / n, 3),
                "avg_slip_bps": round(avg_slip, 2),
                "recent": [[s.get("half_spread_bps", 0.0),
                            s.get("slip_bps", 0.0),
                            1 if s.get("spike") else 0]
                           for s in self.recent_stress]}

    # ------------------------------------------------------------ api
    def equity(self) -> float:
        eq = self.balance
        for p in self.positions.values():
            if self._last_px:
                direction = 1 if p["side"] == "LONG" else -1
                eq += p["size"] * (self._last_px - p["entry"]) * direction
        return eq

    def open_positions(self) -> list[dict]:
        return list(self.positions.values())

    def market_order(self, side: str, size: float, sl: float, tp1: float,
                     module: str, reason: str,
                     fill_px: float | None = None) -> OrderResult:
        """fill_px=None -> taker fill at the live price. When fill_px is set
        (maker-limit touch), the fill happens AT the limit price with MAKER
        fee - mirroring the calibration's post-only entry economics."""
        px = self._last_px
        if not px:
            return OrderResult(False, error="no price feed")
        maker = fill_px is not None
        stress_extra, stress_meta = self._stress(side, maker)
        if maker:
            # maker entry: fee_maker + slippage + latency, filled at the limit
            cost = self.cfg.fee_maker + self.cfg.slippage_rate + stress_extra
            fill = fill_px * (1 + cost) if side == "LONG" else fill_px * (1 - cost)
        else:
            cost = self.cfg.fee_rate + self.cfg.slippage_rate + stress_extra
            fill = px * (1 + cost) if side == "LONG" else px * (1 - cost)
        ticket = uuid.uuid4().hex[:10]
        self.positions[ticket] = {
            "ticket": ticket, "side": side, "size": size, "entry": fill,
            "sl": sl, "tp1": tp1, "module": module, "reason": reason,
            "opened_ts": datetime.now(timezone.utc).isoformat(),
            "tp1_done": False, "be_moved": False,
        }
        log_event(self.cfg, "paper_fill",
                  {"ticket": ticket, "side": side, "size": size,
                   "fill": round(fill, 2), "sl": sl, "tp1": tp1,
                   "maker": fill_px is not None, **stress_meta})
        self._save()
        return OrderResult(True, ticket=ticket, price=fill)

    def close_position(self, ticket: str, price: float, fraction: float = 1.0,
                       reason: str = "") -> tuple[float, bool]:
        """Returns (realized_pnl, fully_closed)."""
        p = self.positions.get(ticket)
        if not p or not price:
            return 0.0, False
        stress_extra, stress_meta = self._stress(p["side"], maker=False)
        self.last_close_meta = ({k: stress_meta[k] for k in
                                 ("half_spread_bps", "spike", "slip_bps")}
                                if stress_meta.get("stress") == "on" else {})
        cost = self.cfg.fee_rate + self.cfg.slippage_rate + stress_extra
        fill = price * (1 - cost) if p["side"] == "LONG" else price * (1 + cost)
        portion = p["size"] * min(fraction, 1.0)
        gross = portion * (fill - p["entry"]) * (1 if p["side"] == "LONG" else -1)
        fees = portion * (fill + p["entry"]) * cost
        pnl = gross - fees
        self.balance += pnl
        if fraction >= 0.999:
            del self.positions[ticket]
            closed = True
        else:
            p["size"] = p["size"] * (1.0 - fraction)   # shrink remaining size
            closed = False
        log_event(self.cfg, "paper_close",
                  {"ticket": ticket, "reason": reason,
                   "exit": round(fill, 2), "pnl": round(pnl, 2),
                   "fraction": fraction, **stress_meta})
        self._save()
        return pnl, closed

    def modify_sl(self, ticket: str, new_sl: float) -> bool:
        p = self.positions.get(ticket)
        if not p:
            return False
        p["sl"] = new_sl
        self._save()
        return True

    def mark_tp1(self, ticket: str) -> None:
        p = self.positions.get(ticket)
        if p:
            p["tp1_done"] = True
            self._save()

    def candles(self, timeframe: str = "1h", limit: int = 400) -> pd.DataFrame:
        """Live paper feed: the freshest tape this machine can get.

        Tape chain (RIPPER_FEED, default `auto`):
          1. Bitget public OHLCV  - the XAU index perp live mode trades
          2. Yahoo GC=F futures   - legacy fallback (hardened)
          3. local history CSV    - fully offline
        Every layer is hard-timeout + single-flight + cache guarded: a
        hung venue can never stall the trading loop.
        """
        now = time.time()
        cached = self._last_feed
        if cached is not None and len(cached) and \
                now - self._last_feed_ts < self.cfg.feed_cache_s:
            self._mark_feed(self._last_feed_source or "cache", cached)
            self.set_price(float(cached["close"].iloc[-1]))
            return cached.tail(limit)

        df, source = self._fetch(timeframe, limit)
        if df is not None and len(df):
            self._last_feed = df
            self._last_feed_ts = now
            self._last_feed_source = source
            self._mark_feed(source, df)
            self.set_price(float(df["close"].iloc[-1]))
            return df.tail(limit)

        # fallback chain: last good download -> local CSV (DEGRADED)
        if self._last_feed is not None and len(self._last_feed):
            self._mark_feed(f"stale {self._last_feed_source}",
                            self._last_feed, degraded=True)
            self.set_price(float(self._last_feed["close"].iloc[-1]))
            return self._last_feed.tail(limit)
        try:
            df = pd.read_csv(self.cfg.live_candle_csv, index_col=0,
                             parse_dates=True)
            df.index = pd.to_datetime(df.index, utc=True)
            self._last_feed = df
            self._last_feed_ts = now
            self._last_feed_source = "local csv"
            self._mark_feed("local csv", df, degraded=True)
            self.set_price(float(df["close"].iloc[-1]))
            return df.tail(limit)
        except Exception:  # noqa: BLE001
            return pd.DataFrame()

    def refresh_feed(self, timeframe: str = "1h") -> dict:
        """feed_check diagnostics: force a fresh tape fetch and report
        exactly which layer answered, how many bars and how fast."""
        t0 = time.time()
        self._last_feed_ts = 0.0
        df, source = self._fetch(timeframe, 400, force=True)
        lat = round(time.time() - t0, 2)
        if df is not None and len(df):
            self._last_feed = df
            self._last_feed_ts = time.time()
            self._last_feed_source = source
            self._mark_feed(source, df)
            self.set_price(float(df["close"].iloc[-1]))
            return {"ok": True, "source": source, "bars": int(len(df)),
                    "latency_s": lat,
                    "px": round(float(df["close"].iloc[-1]), 2)}
        # nothing fresh - report what the machine would still price on
        stale = self._last_feed
        if stale is not None and len(stale):
            return {"ok": False, "source": self._last_feed_source,
                    "bars": int(len(stale)), "latency_s": lat,
                    "stale": True,
                    "px": round(float(stale["close"].iloc[-1]), 2),
                    "note": "all live tapes down - pricing off the "
                            "last good download"}
        return {"ok": False, "source": "none", "bars": 0,
                "latency_s": lat,
                "note": "no tape at all - check network / RIPPER_FEED"}

    def disconnect(self) -> None:
        self._save()
