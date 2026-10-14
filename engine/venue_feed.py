"""
GOLD RIPPER :: Venue Feed
=========================
Public Bitget OHLCV tape - market data needs NO API keys, so even the
PAPER bot can price off the same XAU index perp it would trade on LIVE
(no Yahoo futures-basis drift, no 429 death spirals).

Selected with RIPPER_FEED in .env:
  auto   (default) - venue tape first, then Yahoo GC=F, then local CSV
  venue            - Bitget OHLCV only (XAU/USDT:USDT index perp)
  yahoo            - legacy Yahoo GC=F futures tape
  csv              - the local history CSV only (fully offline)

Hardened exactly like the paper feed (a venue can never stall the loop):
  1. hard timeout (RIPPER_FEED_TIMEOUT_S) on the fetch thread
  2. single-flight lock + small cache (RIPPER_FEED_CACHE_S)
  3. last-good fallback inside the broker's wider chain
"""
from __future__ import annotations

import threading
import time

import pandas as pd

from engine.config import Config
from engine.journal import log_event

RETRY_COOLDOWN_S = 300  # after a connect failure, lie low for 5 minutes


class VenueFeed:
    """ccxt Bitget public OHLCV with the standard ripper hardening."""

    name = "venue"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.x = None
        self.symbol = ""
        self.connected = False
        self.last_latency_s: float | None = None
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._last: pd.DataFrame | None = None
        self._last_ts = 0.0
        self._next_try = 0.0

    # ------------------------------------------------------------ plumbing
    def connect(self) -> bool:
        """Build the public ccxt client and resolve the gold perp symbol."""
        if self.connected:
            return True
        if time.time() < self._next_try:
            return False            # recent failure - don't hammer
        try:
            import ccxt
        except ImportError:
            self.last_error = "ccxt not installed (pip install ccxt)"
            self._next_try = time.time() + RETRY_COOLDOWN_S
            return False
        try:
            self.x = ccxt.bitget({
                "enableRateLimit": True,
                "options": {
                    "defaultType": "swap",
                    "adjustForTimeDifference": True,
                },
            })
            markets = self.x.load_markets()
        except Exception as e:  # noqa: BLE001 - offline / blocked / DNS
            self.last_error = str(e)[:140]
            self._next_try = time.time() + RETRY_COOLDOWN_S
            return False
        cands = ([self.cfg.bitget_symbol]
                 if self.cfg.bitget_symbol
                 else list(self.cfg.bitget_symbol_candidates))
        for s in cands:
            if s in markets:
                self.symbol = s
                break
        if not self.symbol:
            goldish = [s for s in markets
                       if "XAU" in s.upper() or "PAXG" in s.upper()]
            self.last_error = (f"no gold perp among {cands} "
                               f"(gold-like: {goldish[:5]})")
            self._next_try = time.time() + RETRY_COOLDOWN_S
            return False
        self.connected = True
        self.last_error = None
        return True

    # ------------------------------------------------------------ data
    def candles(self, timeframe: str = "1h", limit: int = 400) -> pd.DataFrame:
        """Cached OHLCV; fetches in a hard-timeout thread. Returns the
        last good venue frame (possibly empty when never fetched)."""
        now = time.time()
        if self._last is not None and len(self._last) and \
                now - self._last_ts < self.cfg.feed_cache_s:
            return self._last
        if not self.connected or not self._lock.acquire(blocking=False):
            return self._last if self._last is not None else pd.DataFrame()

        def _dl() -> None:
            try:
                t0 = time.time()
                ohlcv = self.x.fetch_ohlcv(self.symbol, timeframe,
                                           limit=limit)
                df = pd.DataFrame(
                    ohlcv,
                    columns=["ts", "open", "high", "low", "close", "volume"])
                df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                df = df.set_index("ts").dropna()
                if len(df):
                    self._last = df
                    self._last_ts = time.time()
                    self.last_latency_s = round(time.time() - t0, 2)
                    self.last_error = None
            except Exception as e:  # noqa: BLE001 - caller falls back
                self.last_error = str(e)[:140]
            finally:
                try:
                    self._lock.release()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=_dl, daemon=True,
                         name="ripper-venue-feed").start()
        # hard cap: a hung socket can never stall the trading loop
        deadline = time.time() + self.cfg.feed_timeout_s
        while self._lock.locked() and time.time() < deadline:
            time.sleep(0.05)
        return self._last if self._last is not None else pd.DataFrame()

    def refresh_now(self, timeframe: str = "1h",
                    limit: int = 400) -> pd.DataFrame:
        """Force a fresh fetch (busts the cache) - used by feed_check."""
        self._last_ts = 0.0
        return self.candles(timeframe, limit)
