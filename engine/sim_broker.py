"""In-memory broker that behaves like MT5 for testing on any machine.

Buys fill at ask and close at bid; sells the opposite. SL and TP trigger on the
"server" side as soon as price touches them, even if the engine isn't running.
"""
from __future__ import annotations

import itertools

from .broker import AccountInfo, Deal, OrderResult, Position, Quote, SymbolInfo
from .models import Side

TRADE_RETCODE_REQUOTE = 10004
TRADE_RETCODE_INVALID_STOPS = 10016
TRADE_RETCODE_MARKET_CLOSED = 10018


class SimBroker:
    def __init__(self, bid: float = 2650.00, spread: float = 0.30, info: SymbolInfo | None = None) -> None:
        self.info = info or SymbolInfo(tick_size=0.01, digits=2, vol_min=0.01, vol_step=0.01,
                                       vol_max=100.0, stops_level=0.0, freeze_level=0.0)
        self.bid = bid
        self.spread = spread
        self.time = 0.0
        self.is_open = True
        self._positions: dict[int, Position] = {}
        self._deals: dict[int, list[Deal]] = {}
        self._ids = itertools.count(1001)
        self.fail_next: list[tuple[str, int, str]] = []  # (call name, retcode, message)
        self.calls: list[tuple] = []
        self.start_balance = 10_000.0

    # -- test controls

    @property
    def ask(self) -> float:
        return round(self.bid + self.spread, 2)

    def set_price(self, bid: float) -> None:
        """Move the market; SL/TP fire like they would on the broker's server."""
        self.bid = round(bid, 2)
        self.time += 0.25
        for p in list(self._positions.values()):
            px = self.bid if p.side is Side.BUY else self.ask
            s = p.side.sign
            if p.sl is not None and (px - p.sl) * s <= 0:
                self._fill_close(p, p.volume, p.sl, "sl")
            elif p.tp is not None and (px - p.tp) * s >= 0:
                self._fill_close(p, p.volume, p.tp, "tp")

    def fail(self, call: str, retcode: int = TRADE_RETCODE_REQUOTE, message: str = "requote", times: int = 1) -> None:
        self.fail_next += [(call, retcode, message)] * times

    def _maybe_fail(self, call: str) -> OrderResult | None:
        for i, (c, code, msg) in enumerate(self.fail_next):
            if c == call:
                del self.fail_next[i]
                return OrderResult(False, code, msg)
        if not self.is_open:
            return OrderResult(False, TRADE_RETCODE_MARKET_CLOSED, "market closed")
        return None

    # -- Broker interface

    def symbol_info(self) -> SymbolInfo:
        return self.info

    def account(self) -> AccountInfo:
        closed = sum(d.profit for ds in self._deals.values() for d in ds)
        floating = 0.0
        for p in self._positions.values():
            px = self.bid if p.side is Side.BUY else self.ask
            floating += (px - p.price_open) * p.side.sign * p.volume * self.info.contract_size
        balance = round(self.start_balance + closed, 2)
        return AccountInfo(login=0, server="Simulator", currency="USD", balance=balance,
                           equity=round(balance + floating, 2), margin_free=round(balance + floating, 2))

    def quote(self) -> Quote | None:
        return Quote(self.bid, self.ask, self.time)

    def market_open(self) -> bool:
        return self.is_open

    def open(self, side, volume, sl, tp, comment):
        self.calls.append(("open", side, volume, sl, tp))
        if (r := self._maybe_fail("open")):
            return r
        price = self.ask if side is Side.BUY else self.bid
        t = next(self._ids)
        self._positions[t] = Position(t, side, volume, price, sl, tp)
        self._deals[t] = []
        return OrderResult(True, 10009, "done", t, price, volume)

    def close(self, ticket, side, volume):
        self.calls.append(("close", ticket, volume))
        if (r := self._maybe_fail("close")):
            return r
        p = self._positions.get(ticket)
        if p is None:
            return OrderResult(False, 10036, "position closed")
        price = self.bid if p.side is Side.BUY else self.ask
        self._fill_close(p, volume, price, "bot")
        return OrderResult(True, 10009, "done", ticket, price, volume)

    def modify(self, ticket, sl, tp):
        self.calls.append(("modify", ticket, sl, tp))
        if (r := self._maybe_fail("modify")):
            return r
        p = self._positions.get(ticket)
        if p is None:
            return OrderResult(False, 10036, "position closed")
        p.sl, p.tp = sl, tp
        return OrderResult(True, 10009, "done", ticket)

    def position(self, ticket):
        p = self._positions.get(ticket)
        return None if p is None else Position(p.ticket, p.side, p.volume, p.price_open, p.sl, p.tp)

    def positions(self):
        return [self.position(t) for t in self._positions]

    def closing_deals(self, ticket):
        return list(self._deals.get(ticket, []))

    # -- internals

    def _fill_close(self, p: Position, volume: float, price: float, reason: str) -> None:
        volume = min(volume, p.volume)
        profit = round((price - p.price_open) * p.side.sign * volume * self.info.contract_size, 2)
        self._deals[p.ticket].append(Deal(volume, price, profit, reason, self.time))
        p.volume = round(p.volume - volume, 8)
        if p.volume <= 1e-9:
            del self._positions[p.ticket]
