"""
GOLD RIPPER :: Bitget Broker (LIVE)
===================================
ccxt-based execution on Bitget USDT-margined gold perps.

Symbol resolution: Bitget lists a NATIVE XAUUSDT perp (tracks the spot
gold index - no token premium). ccxt candidates are probed in order:
  XAU/USDT:USDT -> XAUT/USDT:USDT -> PAXG/USDT:USDT
(override with BITGET_SYMBOL in .env)

Execution design (learned from the best open-source bots - and now from
the 9-repo audit, see the merge notes inline):
  - market entry with ATTACHED stop-loss + take-profit algo orders
    (venue-side protection survives bot crashes)
  - FULL-POSITION TP/SL PLAN ORDERS re-attached if missing
    (bitget-trading-bot merge: place-tpsl-order pos_profit/pos_loss armor)
  - true-fill re-anchoring: after a fill we poll the position and re-anchor
    the ladder on openPriceAvg, not the intended price (bitget-trading-bot
    846-852 lesson)
  - error ladder: fatal ccxt errors (permission/invalid/insufficient)
    fail FAST, transient ones retry with backoff (freqtrade retrier merge)
  - price-cross guard on every limit (astra-quant merge: a stale limit on
    the wrong side of spot = instant wrong fill)
  - idempotency via tagged clientOid - a retried order never doubles
  - one-way position mode + leverage configured at connect
  - software fallback: if venue-side SL fails, ripper.py closes
    positions itself on bar boundaries (never naked)
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import pandas as pd

from brokers.base import OrderResult
from engine import guards
from engine.config import Config
from engine.journal import log_event

# freqtrade retrier merge: these keys mean the call can NEVER succeed on
# retry - fail immediately and surface the real problem to the operator
_FATAL_ERR_KEYS = ("permission", "invalid", "insufficient", "parameter",
                   "apikey", "api key", "signature", "balance", "maintenance",
                   "delisted", "not found", "unauthorized")


def _is_fatal(err: str) -> bool:
    low = str(err).lower()
    return any(k in low for k in _FATAL_ERR_KEYS)


class BitgetBroker:
    name = "BITGET"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.x = None
        self.symbol = cfg.bitget_symbol or ""
        self.amount_precision = 3
        self.min_amount = 0.0
        self.connected = False
        self.feed_meta: dict = {"source": "none", "bars": 0}

    # ------------------------------------------------------------ plumbing
    def _retry(self, fn, *args, attempts: int = 3, **kwargs):
        """Retry with backoff - but ONLY transient errors. A permission/
        invalid-order/insufficient-balance error can never succeed on a
        retry, so failing fast surfaces the true fault instead of burning
        3 attempts against a wall (freqtrade exception-ladder merge)."""
        last = None
        for i in range(attempts):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                last = e
                if _is_fatal(str(e)):
                    raise
                time.sleep(min(2 ** i, 8))
        raise last  # type: ignore[misc]

    def connect(self) -> bool:
        try:
            import ccxt
        except ImportError:
            print("[BITGET] ccxt not installed - pip install ccxt")
            return False
        if not (self.cfg.bitget_key and self.cfg.bitget_secret
                and self.cfg.bitget_passphrase):
            print("[BITGET] missing API keys in .env - refusing live mode")
            return False
        self.x = ccxt.bitget({
            "apiKey": self.cfg.bitget_key,
            "secret": self.cfg.bitget_secret,
            "password": self.cfg.bitget_passphrase,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
                # sync against the venue clock on every request - kills the
                # classic "timestamp out of recvWindow" failure on servers
                # with drifted clocks
                "adjustForTimeDifference": True,
                "recvWindow": self.cfg.bitget_recv_window,
            },
        })
        try:
            markets = self._retry(self.x.load_markets)
        except Exception as e:  # noqa: BLE001
            print(f"[BITGET] load_markets failed: {e}")
            return False

        cands = ([self.cfg.bitget_symbol]
                 if self.cfg.bitget_symbol
                 else list(self.cfg.bitget_symbol_candidates))
        chosen = None
        for sym in cands:
            if sym in markets:
                chosen = sym
                break
        if not chosen:
            goldish = [s for s in markets if "XAU" in s.upper()
                       or "PAXG" in s.upper()]
            print(f"[BITGET] no gold perp found among {cands}. "
                  f"Gold-like symbols: {goldish[:8]}")
            return False
        self.symbol = chosen
        m = markets[chosen]
        self.amount_precision = m.get("precision", {}).get("amount", 3) or 3
        self.min_amount = m.get("limits", {}).get("amount", {}).get("min", 0) or 0

        try:
            self._retry(self.x.set_position_mode, False)      # one-way
        except Exception:  # noqa: BLE001
            pass  # some accounts already one-way; ignore
        try:
            self._retry(self.x.set_leverage, self.cfg.bitget_leverage,
                        self.symbol)
        except Exception as e:  # noqa: BLE001
            print(f"[BITGET] leverage set skipped: {e}")

        self.connected = True
        print(f"[BITGET] connected | symbol={self.symbol} "
              f"lev={self.cfg.bitget_leverage}x min_amt={self.min_amount}")
        log_event(self.cfg, "bitget_connect", {"symbol": self.symbol})
        return True

    # ------------------------------------------------------------ data
    def candles(self, timeframe: str = "1h", limit: int = 400) -> pd.DataFrame:
        if not self.connected:
            return pd.DataFrame()
        try:
            t0 = time.time()
            ohlcv = self._retry(self.x.fetch_ohlcv, self.symbol,
                                timeframe, limit=limit, attempts=2)
            df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low",
                                              "close", "volume"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df = df.set_index("ts")
            # venue tape telemetry for the dashboard heartbeat
            self.feed_meta = {
                "source": f"bitget {self.symbol}",
                "bars": int(len(df)),
                "age_s": round(time.time() - t0, 1),
                "last_bar": str(df.index[-1]) if len(df) else "",
                "px": (round(float(df["close"].iloc[-1]), 2)
                       if len(df) else None),
                "degraded": False,
            }
            return df
        except Exception as e:  # noqa: BLE001
            self.feed_meta = {**getattr(self, "feed_meta", {}),
                              "degraded": True,
                              "error": str(e)[:120]}
            log_event(self.cfg, "bitget_candles_error", {"error": str(e)})
            return pd.DataFrame()

    def refresh_feed(self, timeframe: str = "1h") -> dict:
        """feed_check diagnostics (LIVE): force a fresh venue OHLCV pull."""
        t0 = time.time()
        df = self.candles(timeframe, 400)
        lat = round(time.time() - t0, 2)
        if df is not None and len(df):
            return {"ok": True, "source": f"bitget {self.symbol}",
                    "bars": int(len(df)), "latency_s": lat,
                    "px": round(float(df["close"].iloc[-1]), 2)}
        return {"ok": False, "source": f"bitget {self.symbol}",
                "bars": 0, "latency_s": lat,
                "note": "venue candles failed - check connectivity / "
                        "whether the symbol is still listed"}

    def equity(self) -> float | None:
        try:
            bal = self._retry(self.x.fetch_balance)
            usdt = bal.get("total", {}).get("USDT")
            return float(usdt) if usdt else None
        except Exception:  # noqa: BLE001
            return None

    def account(self) -> dict | None:
        """USDT margin snapshot for the dashboard heartbeat."""
        try:
            bal = self._retry(self.x.fetch_balance)
            total = bal.get("total", {}).get("USDT")
            free = bal.get("free", {}).get("USDT")
            used = bal.get("used", {}).get("USDT")
            if total is None:
                return None
            return {"total": round(float(total), 2),
                    "free": round(float(free or 0), 2),
                    "used": round(float(used or 0), 2)}
        except Exception:  # noqa: BLE001
            return None

    def last_price(self) -> float | None:
        try:
            t = self._retry(self.x.fetch_ticker, self.symbol, attempts=2)
            return float(t.get("last") or 0) or None
        except Exception:  # noqa: BLE001
            return None

    def spread_bps(self) -> float | None:
        """Live book spread in bps for the pre-entry SpreadFilter guard
        (freqtrade merge). None = book unavailable (paper fallback)."""
        try:
            t = self._retry(self.x.fetch_ticker, self.symbol, attempts=1)
            bid, ask = t.get("bid"), t.get("ask")
            if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
                return None
            return (float(ask) - float(bid)) / ((float(ask) + float(bid)) / 2) * 10_000
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ fills
    def recent_fills(self, since_iso: str | None = None) -> list[dict]:
        """Venue fill history for engine/reconcile.py: normalized trades
        since an ISO stamp (None -> last 24h). reduce_only=True marks the
        venue-side algo exits (SL/TP) - the ladder never journals those."""
        since_ms = None
        if since_iso:
            try:
                from datetime import datetime
                since_ms = int(datetime.fromisoformat(
                    str(since_iso).replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:  # noqa: BLE001
                since_ms = None
        trades = self._retry(self.x.fetch_my_trades, self.symbol,
                             since=since_ms, limit=200, attempts=2) or []
        out = []
        for t in trades[-200:]:
            ts_ms = t.get("timestamp") or t.get("datetime")
            epoch = (float(ts_ms) / 1000 if isinstance(ts_ms, (int, float))
                     else 0.0)
            iso = t.get("datetime") or ""
            out.append({
                "ts": epoch,
                "iso": iso,
                "side": str(t.get("side") or "").upper(),   # BUY / SELL
                "px": float(t.get("price") or 0),
                "amount": float(t.get("amount") or 0),
                # fee in USDT (negative = paid); venue exits pay too
                "fee": float((t.get("fee") or {}).get("cost") or 0),
                "reduce_only": bool(
                    (t.get("info") or {}).get("reduceOnly")
                    in ("true", True, "1")
                    or (t.get("info") or {}).get("reduce_only")
                    in ("true", True, "1")),
                "oid": str(t.get("order") or t.get("id") or ""),
            })
        return out

    # ------------------------------------------------------------ positions
    def open_positions(self) -> list[dict]:
        if not self.connected:
            return []
        try:
            poss = self._retry(self.x.fetch_positions, [self.symbol],
                               attempts=2)
            out = []
            for p in poss:
                size = float(p.get("contracts") or 0)
                if size <= 0:
                    continue
                side = "LONG" if str(p.get("side")) == "long" else "SHORT"
                out.append({
                    "ticket": p.get("id") or self.symbol,
                    "side": side,
                    "size": size,
                    "entry": float(p.get("entryPrice") or 0),
                    "sl": float(p.get("stopLossPrice") or 0),
                    "tp1": float(p.get("takeProfitPrice") or 0),
                    "module": "live",
                })
            return out
        except Exception as e:  # noqa: BLE001
            log_event(self.cfg, "bitget_positions_error", {"error": str(e)})
            return []

    # ------------------------------------------------------------ orders
    @staticmethod
    def _client_oid(tag: str) -> str:
        """Tagged idempotency key: fill attribution survives the venue's
        order history (passivbot 0x<type-id> lesson, ripper flavor)."""
        return f"r{tag[:3].lower()}{uuid.uuid4().hex[:16]}"

    def market_order(self, side: str, size: float, sl: float, tp1: float,
                     module: str, reason: str) -> OrderResult:
        if not self.connected:
            return OrderResult(False, error="not connected")
        try:
            amount = float(self.x.amount_to_precision(self.symbol, size))
            if self.min_amount and amount < self.min_amount:
                return OrderResult(
                    False, error=f"size {amount} below venue min "
                                 f"{self.min_amount} - widen SL distance or "
                                 f"increase RISK_USD_PER_TRADE")
            oside = "buy" if side == "LONG" else "sell"
            params = {
                "stopLossPrice": float(
                    self.x.price_to_precision(self.symbol, sl)),
                "takeProfitPrice": float(
                    self.x.price_to_precision(self.symbol, tp1)),
                "clientOid": self._client_oid(module),
            }
            o = self._retry(self.x.create_order, self.symbol, "market",
                            oside, amount, None, params, attempts=2)
            px = float(o.get("average") or o.get("price") or 0)
            # TRUE-FILL RE-ANCHOR (bitget-trading-bot 846-852 merge): the
            # venue's openPriceAvg is the real fill, not our intended
            # price - poll briefly and re-anchor the ladder on it.
            real = self._position_avg_entry(oside, tries=3)
            if real:
                px = real
            log_event(self.cfg, "bitget_fill",
                      {"side": side, "amount": amount, "price": px,
                       "sl": sl, "tp1": tp1, "module": module,
                       "reason": reason})
            return OrderResult(True, ticket=str(o.get("id", "")), price=px)
        except Exception as e:  # noqa: BLE001
            err = str(e)
            log_event(self.cfg, "bitget_order_error", {"error": err})
            hint = ""
            low = err.lower()
            if "permission" in low:
                hint = " | check API key has Futures-Trade permission"
            elif "ip" in low:
                hint = " | add your server IP to the Bitget API whitelist"
            elif "timestamp" in low or "recv_window" in low or \
                    "recvwindow" in low:
                hint = (" | clock skew - enable NTP sync or raise "
                        "BITGET_RECV_WINDOW in .env")
            return OrderResult(False, error=err + hint)

    def _position_avg_entry(self, oside: str, tries: int = 3) -> float | None:
        """Poll the position for openPriceAvg right after a fill (the fill
        can take a beat to settle) - None = keep the intended price."""
        want = "long" if oside == "buy" else "short"
        for _ in range(tries):
            try:
                time.sleep(0.2)
                for p in self._retry(self.x.fetch_positions, [self.symbol],
                                     attempts=1):
                    if str(p.get("side")) == want and \
                            float(p.get("contracts") or 0) > 0:
                        v = float(p.get("entryPrice") or 0)
                        if v > 0:
                            return v
            except Exception:  # noqa: BLE001
                pass
        return None

    # -- maker path: post-only limit with attached SL/TP -------------------
    def limit_order(self, side: str, size: float, px: float, sl: float,
                    tp1: float, module: str, reason: str) -> OrderResult:
        """Place a post-only limit (maker 0.02% fee) with venue-side SL/TP
        attached. The algo legs activate on the base fill, so protection is
        continuous from the first fill - the order can never be naked.

        PRICE-CROSS GUARD (astra-quant merge): a limit that crosses (or
        absurdly over-reaches) spot is a stale-signal hallucination -
        refused here so the caller can take the honest taker path."""
        if not self.connected:
            return OrderResult(False, error="not connected")
        spot = self.last_price()
        ok, why = guards.price_cross_ok(side, float(px), spot or 0.0)
        if not ok:
            log_event(self.cfg, "price_cross_refused",
                      {"side": side, "px": px, "spot": spot, "why": why})
            return OrderResult(False, error=f"price-cross guard: {why}")
        try:
            amount = float(self.x.amount_to_precision(self.symbol, size))
            if self.min_amount and amount < self.min_amount:
                return OrderResult(
                    False, error=f"size {amount} below venue min "
                                 f"{self.min_amount}")
            oside = "buy" if side == "LONG" else "sell"
            params = {
                "postOnly": True,
                "stopLossPrice": float(
                    self.x.price_to_precision(self.symbol, sl)),
                "takeProfitPrice": float(
                    self.x.price_to_precision(self.symbol, tp1)),
                "clientOid": self._client_oid(module),
            }
            o = self._retry(
                self.x.create_order, self.symbol, "limit", oside, amount,
                float(self.x.price_to_precision(self.symbol, px)), params,
                attempts=2)
            oid = str(o.get("id", ""))
            log_event(self.cfg, "bitget_limit_placed",
                      {"side": side, "amount": amount, "px": px, "sl": sl,
                       "tp1": tp1, "module": module, "reason": reason,
                       "oid": oid})
            return OrderResult(True, ticket=oid, price=px)
        except Exception as e:  # noqa: BLE001
            err = str(e)
            log_event(self.cfg, "bitget_limit_error", {"error": err})
            return OrderResult(False, error=err)

    def limit_status(self, oid: str) -> tuple[str, float]:
        """Return (state, avg_fill_px) for a placed limit order.
        state: open | filled | canceled | unknown"""
        if not self.connected or not oid:
            return "unknown", 0.0
        try:
            o = self._retry(self.x.fetch_order, oid, self.symbol, attempts=2)
            status = str(o.get("status", "unknown"))
            px = float(o.get("average") or o.get("price") or 0)
            if status == "closed" or o.get("filled"):
                return "filled", px
            if status in ("open", ""):
                return "open", px
            return ("canceled" if status in ("canceled", "cancelled",)
                    else status or "unknown"), px
        except Exception:  # noqa: BLE001
            return "unknown", 0.0

    def cancel_limit(self, oid: str) -> bool:
        if not self.connected or not oid:
            return False
        try:
            self._retry(self.x.cancel_order, oid, self.symbol, attempts=2)
            log_event(self.cfg, "bitget_limit_canceled", {"oid": oid})
            return True
        except Exception:  # noqa: BLE001
            return False

    def set_stop(self, size: float, new_sl: float, side: str) -> bool:
        """Cancel venue stops and re-place a reduce-only trigger SL."""
        if not self.connected:
            return False
        try:
            try:
                self._retry(self.x.cancel_all_orders, self.symbol, attempts=1)
            except Exception:  # noqa: BLE001
                pass
            close_side = "sell" if side == "LONG" else "buy"
            amount = float(self.x.amount_to_precision(self.symbol, size))
            self._retry(
                self.x.create_order, self.symbol, "market", close_side,
                amount, None,
                {
                    "stopLossPrice": float(self.x.price_to_precision(
                        self.symbol, new_sl)),
                    "reduceOnly": True,
                    "clientOid": uuid.uuid4().hex,
                },
                attempts=2)
            return True
        except Exception as e:  # noqa: BLE001
            log_event(self.cfg, "bitget_stop_error", {"error": str(e)})
            return False

    def close_position(self, side: str, size: float) -> OrderResult:
        if not self.connected:
            return OrderResult(False, error="not connected")
        try:
            amount = float(self.x.amount_to_precision(self.symbol, size))
            close_side = "sell" if side == "LONG" else "buy"
            o = self._retry(
                self.x.create_order, self.symbol, "market", close_side,
                amount, None,
                {"reduceOnly": True, "clientOid": self._client_oid("close")},
                attempts=2)
            return OrderResult(True, ticket=str(o.get("id", "")),
                               price=float(o.get("average") or 0))
        except Exception as e:  # noqa: BLE001
            return OrderResult(False, error=str(e))

    # ------------------------------------------------------------ plan armor
    # bitget-trading-bot merge (416-509 + 723-766): full-position TP/SL
    # PLAN orders survive bot crashes AND a manual venue-app close. We
    # mirror them here so the ladder's SL can never silently vanish; a
    # missing leg is re-placed by ensure_plans() on the ripper loop.
    def pending_plans(self) -> list[dict]:
        """List pending TP/SL plan orders (planType=profit_loss)."""
        if not self.connected:
            return []
        try:
            raw = self._retry(
                self.x.private_get_mix_order_orders_plan_pending,
                {"symbol": self.x.market_id(self.symbol),
                 "productType": "USDT-FUTURES",
                 "planType": "profit_loss"}, attempts=1)
            out = []
            for e in (raw or {}).get("data", {}).get("entrustedList") or []:
                out.append({"plan_id": e.get("planId"),
                            "kind": str(e.get("planType", "")),
                            "side": str(e.get("holdSide", "")),
                            "size": float(e.get("size") or 0),
                            "trigger": float(e.get("triggerPrice") or 0)})
            return out
        except Exception as e:  # noqa: BLE001
            log_event(self.cfg, "plans_pending_error", {"error": str(e)[:120]})
            return []

    def place_pos_plans(self, side: str, size: float, sl: float,
                        tp1: float, legs: set | None = None) -> bool:
        """(Re-)attach full-position profit/loss plan orders for an open
        position. Only the MISSING legs are placed (never double-protect).
        Non-fatal: failure just means the software ladder stays the only
        exit (it already is, on paper)."""
        if not self.connected:
            return False
        want = legs or {"pos_loss", "pos_profit"}
        try:
            amount = float(self.x.amount_to_precision(self.symbol, size))
            hold_side = "long" if side == "LONG" else "short"
            placed = 0
            if "pos_loss" in want:
                body = {
                    "symbol": self.x.market_id(self.symbol),
                    "productType": "USDT-FUTURES",
                    "marginCoin": self.cfg.bitget_margin_coin,
                    "planType": "pos_loss",
                    "triggerPrice": self.x.price_to_precision(self.symbol, sl),
                    "triggerType": "mark_price",
                    "executePrice": "0",
                    "holdSide": hold_side,
                    "size": str(amount),
                    "clientOid": self._client_oid("planloss"),
                }
                self._retry(self.x.private_post_mix_order_place_tpsl_order,
                            body, attempts=1)
                placed += 1
            if "pos_profit" in want:
                body = {
                    "symbol": self.x.market_id(self.symbol),
                    "productType": "USDT-FUTURES",
                    "marginCoin": self.cfg.bitget_margin_coin,
                    "planType": "pos_profit",
                    "triggerPrice": self.x.price_to_precision(self.symbol, tp1),
                    "triggerType": "mark_price",
                    "executePrice": "0",
                    "holdSide": hold_side,
                    "size": str(amount),
                    "clientOid": self._client_oid("planprof"),
                }
                self._retry(self.x.private_post_mix_order_place_tpsl_order,
                            body, attempts=1)
                placed += 1
            if placed:
                log_event(self.cfg, "plans_attached",
                          {"side": side, "size": amount, "sl": sl,
                           "tp1": tp1, "legs": sorted(want)})
            return placed > 0
        except Exception as e:  # noqa: BLE001
            log_event(self.cfg, "plans_attach_error", {"error": str(e)[:120]})
            return False

    def disconnect(self) -> None:
        self.connected = False
