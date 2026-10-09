"""MetaTrader 5 adapter. Runs only on Windows, next to a logged-in MT5 terminal.

Needs: pip install MetaTrader5, and "Algo Trading" enabled in the terminal.
"""
from __future__ import annotations

import time

import MetaTrader5 as mt5

from .broker import AccountInfo, Deal, OrderResult, Position, Quote, SymbolInfo
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

    def account(self) -> AccountInfo:
        a = mt5.account_info()
        term = mt5.terminal_info()
        if a is None:
            return AccountInfo(0, "", "USD", 0.0, 0.0, 0.0, connected=False)
        return AccountInfo(a.login, a.server, a.currency, a.balance, a.equity, a.margin_free,
                           connected=bool(term and term.connected))

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
            p = self._wait_position(r.ticket)
            r.price = p.price_open if p else req["price"]
        return r

    def close(self, ticket: int, side: Side, volume: float) -> OrderResult:
        before = self.position(ticket)
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
        if not r.ok and before is not None:
            # Capital.com sometimes answers a filled close with retcode 0 "Done". Check the position itself.
            deadline = time.time() + 3.0
            while time.time() < deadline:
                after = self.position(ticket)
                if after is None or after.volume < before.volume - 1e-9:
                    r.ok, r.message = True, "done (verified on the terminal)"
                    break
                time.sleep(0.1)
        if r.ok and not r.price:
            time.sleep(0.2)
            r.price = self._last_out_price(ticket, req["price"])
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

    _OK_CODES = (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_DONE_PARTIAL)

    def _send(self, req: dict) -> OrderResult:
        r = mt5.order_send(req)
        if r is None:
            code, msg = mt5.last_error()
            return OrderResult(False, code, msg)
        ok = r.retcode in self._OK_CODES
        if not ok and r.order and req["action"] == mt5.TRADE_ACTION_DEAL:
            # Some brokers (Capital.com) answer with an unexpected code even though the order filled.
            # Trust the terminal's state over the return code: if a position/deal exists for this order, it went through.
            if self._wait_position(r.order) or self._order_deals(r.order):
                ok = True
        if not ok:
            return OrderResult(False, r.retcode, f"{r.comment or 'rejected'} (retcode {r.retcode})")
        return OrderResult(True, r.retcode, r.comment, r.order, r.price or None, r.volume)

    def _wait_position(self, ticket: int, timeout: float = 3.0) -> Position | None:
        """Market orders can be reported before the position shows up; poll briefly."""
        deadline = time.time() + timeout
        while True:
            p = self.position(ticket)
            if p is not None or time.time() >= deadline:
                return p
            time.sleep(0.1)

    @staticmethod
    def _order_deals(order: int) -> list:
        return list(mt5.history_deals_get(order=order) or [])

    def _last_out_price(self, ticket: int, fallback: float) -> float:
        deals = [d for d in (mt5.history_deals_get(position=ticket) or [])
                 if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
        return deals[-1].price if deals else fallback

    @staticmethod
    def _pos(p) -> Position:
        side = Side.BUY if p.type == mt5.POSITION_TYPE_BUY else Side.SELL
        return Position(p.ticket, side, p.volume, p.price_open, p.sl or None, p.tp or None)
