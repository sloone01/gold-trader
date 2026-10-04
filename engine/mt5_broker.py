"""MetaTrader 5 adapter. Runs only on Windows, next to a logged-in MT5 terminal.

Needs: pip install MetaTrader5, and "Algo Trading" enabled in the terminal.
"""
from __future__ import annotations

import time

import MetaTrader5 as mt5

from .broker import Deal, OrderResult, Position, Quote, SymbolInfo
from .models import Side

MAGIC = 260210          # tags this bot's orders so they're easy to find in MT5
DEVIATION_POINTS = 30   # max slippage accepted on market orders

_REASONS = {
    mt5.DEAL_REASON_SL: "sl",
    mt5.DEAL_REASON_TP: "tp",
    mt5.DEAL_REASON_SO: "stop out",
    mt5.DEAL_REASON_CLIENT: "manual",
    mt5.DEAL_REASON_MOBILE: "manual",
    mt5.DEAL_REASON_WEB: "manual",
    mt5.DEAL_REASON_EXPERT: "bot",
}


class MT5Error(RuntimeError):
    pass


class MT5Broker:
    def __init__(self, symbol: str = "XAUUSD", login: int | None = None, password: str | None = None,
                 server: str | None = None, terminal_path: str | None = None) -> None:
        kwargs = {k: v for k, v in dict(login=login, password=password, server=server).items() if v}
        ok = mt5.initialize(terminal_path, **kwargs) if terminal_path else mt5.initialize(**kwargs)
        if not ok:
            raise MT5Error(f"MT5 initialize failed: {mt5.last_error()}")
        acc = mt5.account_info()
        if acc.margin_mode != mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            raise MT5Error("This account is in netting mode. The bot needs a hedging account so each trade "
                           "is its own position.")
        if not mt5.symbol_select(symbol, True):
            raise MT5Error(f"Symbol {symbol} not found. Check your broker's gold symbol name (e.g. XAUUSD, GOLD).")
        self.symbol = symbol
        s = mt5.symbol_info(symbol)
        self._point = s.point
        self._filling = (mt5.ORDER_FILLING_FOK if s.filling_mode & 1 else
                         mt5.ORDER_FILLING_IOC if s.filling_mode & 2 else mt5.ORDER_FILLING_RETURN)

    def symbol_info(self) -> SymbolInfo:
        s = mt5.symbol_info(self.symbol)
        return SymbolInfo(
            tick_size=s.trade_tick_size, digits=s.digits,
            vol_min=s.volume_min, vol_step=s.volume_step, vol_max=s.volume_max,
            stops_level=s.trade_stops_level * s.point, freeze_level=s.trade_freeze_level * s.point,
            contract_size=s.trade_contract_size,
        )

    def quote(self) -> Quote | None:
        t = mt5.symbol_info_tick(self.symbol)
        return None if t is None or t.bid == 0 else Quote(t.bid, t.ask, t.time_msc / 1000)

    def market_open(self) -> bool:
        s = mt5.symbol_info(self.symbol)
        if s is None or s.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
            return False
        t = mt5.symbol_info_tick(self.symbol)
        # No tick for a few minutes on a weekday-only symbol means the session is closed.
        return t is not None and time.time() - t.time < 300

    def open(self, side: Side, volume: float, sl: float | None, tp: float | None, comment: str) -> OrderResult:
        t = mt5.symbol_info_tick(self.symbol)
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol, "volume": float(volume),
            "type": mt5.ORDER_TYPE_BUY if side is Side.BUY else mt5.ORDER_TYPE_SELL,
            "price": t.ask if side is Side.BUY else t.bid,
            "sl": float(sl or 0.0), "tp": float(tp or 0.0),
            "deviation": DEVIATION_POINTS, "magic": MAGIC, "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling,
        }
        r = self._send(req)
        # In MT5 the position ticket equals the ticket of the order that opened it.
        if r.ok and not r.price:
            p = self.position(r.ticket)
            r.price = p.price_open if p else req["price"]
        return r

    def close(self, ticket: int, side: Side, volume: float) -> OrderResult:
        t = mt5.symbol_info_tick(self.symbol)
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol, "position": ticket, "volume": float(volume),
            "type": mt5.ORDER_TYPE_SELL if side is Side.BUY else mt5.ORDER_TYPE_BUY,
            "price": t.bid if side is Side.BUY else t.ask,
            "deviation": DEVIATION_POINTS, "magic": MAGIC, "comment": "gold-trader close",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling,
        }
        r = self._send(req)
        r.ticket = ticket
        return r

    def modify(self, ticket: int, sl: float | None, tp: float | None) -> OrderResult:
        req = {"action": mt5.TRADE_ACTION_SLTP, "symbol": self.symbol, "position": ticket,
               "sl": float(sl or 0.0), "tp": float(tp or 0.0), "magic": MAGIC}
        r = self._send(req)
        r.ticket = ticket
        return r

    def position(self, ticket: int) -> Position | None:
        ps = mt5.positions_get(ticket=ticket)
        return self._pos(ps[0]) if ps else None

    def positions(self) -> list[Position]:
        return [self._pos(p) for p in (mt5.positions_get(symbol=self.symbol) or []) if p.magic == MAGIC]

    def closing_deals(self, ticket: int) -> list[Deal]:
        deals = mt5.history_deals_get(position=ticket) or []
        out = []
        for d in deals:
            if d.entry not in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY):
                continue
            reason = _REASONS.get(d.reason, "manual")
            if reason == "manual" and d.magic == MAGIC:
                reason = "bot"
            out.append(Deal(d.volume, d.price, d.profit + d.commission + d.swap + d.fee, reason, d.time_msc / 1000))
        return sorted(out, key=lambda x: x.time)

    # -- internals

    def _send(self, req: dict) -> OrderResult:
        r = mt5.order_send(req)
        if r is None:
            code, msg = mt5.last_error()
            return OrderResult(False, code, msg)
        if r.retcode != mt5.TRADE_RETCODE_DONE:
            return OrderResult(False, r.retcode, r.comment or f"retcode {r.retcode}")
        return OrderResult(True, r.retcode, r.comment, r.order, r.price or None, r.volume)

    @staticmethod
    def _pos(p) -> Position:
        side = Side.BUY if p.type == mt5.POSITION_TYPE_BUY else Side.SELL
        return Position(p.ticket, side, p.volume, p.price_open, p.sl or None, p.tp or None)
