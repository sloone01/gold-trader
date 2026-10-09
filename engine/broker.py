"""Broker interface. The real MT5 adapter and the simulator both implement it."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import Side


@dataclass
class SymbolInfo:
    tick_size: float
    digits: int
    vol_min: float
    vol_step: float
    vol_max: float
    stops_level: float   # minimum SL/TP distance from price, as a PRICE distance
    freeze_level: float  # no modifications this close to SL/TP, as a PRICE distance
    contract_size: float = 100.0


@dataclass
class Quote:
    bid: float
    ask: float
    time: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class OrderResult:
    ok: bool
    retcode: int = 0
    message: str = ""
    ticket: int | None = None
    price: float | None = None
    volume: float | None = None


@dataclass
class Position:
    ticket: int
    side: Side
    volume: float
    price_open: float
    sl: float | None
    tp: float | None


@dataclass
class Deal:
    volume: float
    price: float
    profit: float      # includes commission and swap
    reason: str        # sl | tp | manual | bot
    time: float


@dataclass
class AccountInfo:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    margin_free: float
    connected: bool = True


class Broker(Protocol):
    def symbol_info(self) -> SymbolInfo: ...
    def account(self) -> AccountInfo: ...
    def quote(self) -> Quote | None: ...
    def market_open(self) -> bool: ...
    def open(self, side: Side, volume: float, sl: float | None, tp: float | None, comment: str) -> OrderResult: ...
    def close(self, ticket: int, side: Side, volume: float) -> OrderResult: ...
    def modify(self, ticket: int, sl: float | None, tp: float | None) -> OrderResult: ...
    def position(self, ticket: int) -> Position | None: ...
    def positions(self) -> list[Position]: ...
    def closing_deals(self, ticket: int) -> list[Deal]: ...
