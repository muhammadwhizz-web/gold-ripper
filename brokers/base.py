"""
GOLD RIPPER :: Broker Base
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OrderResult:
    ok: bool
    ticket: str = ""
    price: float = 0.0
    error: str = ""


@dataclass
class OpenPosition:
    ticket: str
    side: str              # LONG | SHORT
    size: float
    entry: float
    sl: float
    tp1: float
    module: str = ""
    opened_ts=None
    tp1_done: bool = False
    be_moved: bool = False
    meta: dict = None
