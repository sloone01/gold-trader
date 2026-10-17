"""Multi-symbol MT5 access for the Markets auto-trader (silver, oil, forex, crypto).

This is the old Market Watch bridge, now in-process: the same MetaTrader5 connection the gold
engine uses, but for any symbol, with risk-% lot sizing, partial-TP tickets, a margin cap and
a real-account guard. Every method returns plain dicts (the bridge's JSON shapes).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

TF = {"5m": "TIMEFRAME_M5", "15m": "TIMEFRAME_M15", "1h": "TIMEFRAME_H1", "4h": "TIMEFRAME_H4", "1d": "TIMEFRAME_D1"}


class TradeRejected(RuntimeError):
    pass


class MT5Multi:
    def __init__(self, allow_live: bool = False) -> None:
        self.allow_live = allow_live
        if mt5.terminal_info() is None and not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

    # --------------------------------------------------------------- reads

    def account(self) -> dict:
        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"account_info failed: {mt5.last_error()}")
        demo = int(info.trade_mode) == 0          # 0 demo, 1 contest, 2 real
        return {"login": info.login, "server": info.server, "balance": info.balance, "equity": info.equity,
                "margin": info.margin, "margin_free": info.margin_free, "currency": info.currency, "is_demo": demo}

    def positions(self, magic: int | None = None) -> list[dict]:
        out = []
        for p in mt5.positions_get() or []:
            if magic is not None and int(p.magic) != magic:
                continue
            out.append({"ticket": p.ticket, "symbol": p.symbol, "type": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                        "volume": p.volume, "price_open": p.price_open, "price_current": p.price_current, "sl": p.sl,
                        "tp": p.tp, "profit": p.profit, "time": int(p.time), "magic": int(p.magic), "comment": p.comment})
        return out

    def candles(self, symbol: str, tf: str, count: int = 200) -> dict:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"cannot select {symbol}")
        rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, TF[tf]), 0, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"no rates for {symbol}: {mt5.last_error()}")
        candles = [{"time": datetime.fromtimestamp(int(r["time"]), tz=timezone.utc).isoformat(), "open": float(r["open"]),
                    "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"])} for r in rates]
        tick = mt5.symbol_info_tick(symbol)
        return {"candles": candles, "price": float(tick.bid) if tick and tick.bid else candles[-1]["close"], "source": "mt5"}

    def deals(self, days: int = 7, magic: int | None = None) -> list[dict]:
        now = datetime.now()
        out = []
        for d in mt5.history_deals_get(now - timedelta(days=days), now + timedelta(hours=12)) or []:
            if not d.symbol or (magic is not None and int(d.magic) != magic):
                continue
            out.append({"ticket": d.ticket, "position_id": d.position_id, "symbol": d.symbol, "type": int(d.type),
                        "entry": int(d.entry), "time": int(d.time), "volume": float(d.volume), "price": float(d.price),
                        "profit": float(d.profit), "commission": float(d.commission), "swap": float(d.swap),
                        "fee": float(d.fee), "magic": int(d.magic), "comment": d.comment})
        return out

    # -------------------------------------------------------------- writes

    def _guard(self) -> None:
        acc = self.account()
        if not acc["is_demo"] and not self.allow_live:
            raise TradeRejected(f"account {acc['login']} ({acc['server']}) is REAL: set AUTO_TRADE_ALLOW_LIVE=true "
                                "only if you really mean to auto-trade it")

    def order(self, symbol: str, side: str, stop: float, take_profit: float | None = None, entry: float | None = None,
              risk_percent: float = 0.5, volume: float | None = None, risk_base_balance: float = 0,
              max_margin_pct: float = 0, max_spread_risk_pct: float = 0, tp_levels: list[dict] | None = None,
              magic: int = 260731, comment: str = "MarketWatch", deviation: int = 20) -> dict:
        self._guard()
        if not mt5.symbol_select(symbol, True):
            raise TradeRejected(f"cannot select {symbol}")
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise TradeRejected(f"no tick for {symbol}")
        otype = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        price = tick.ask if side == "buy" else tick.bid
        stop, tp = self._rebase(symbol, side, price, stop, take_profit or 0, entry)
        risk = abs(price - stop)
        if max_spread_risk_pct > 0 and risk > 0 and tick.ask - tick.bid > risk * max_spread_risk_pct / 100:
            raise TradeRejected(f"spread too wide: {tick.ask - tick.bid:.5f} is over {max_spread_risk_pct:.0f}% of the stop distance")
        vol = self._lot(symbol, stop, risk_percent, volume, price, risk_base_balance)
        if max_margin_pct > 0:
            vol = self._cap_margin(symbol, otype, vol, price, max_margin_pct)
        spec = mt5.symbol_info(symbol)
        digits = int(spec.digits or 5)
        parts = self._split(symbol, vol, tp_levels) if tp_levels and risk > 0 else [(vol, 0.0)]
        orders, ok_all = [], True
        for i, (pv, r) in enumerate(parts, 1):
            ptp = round(price + risk * r if side == "buy" else price - risk * r, digits) if r > 0 else (tp or 0.0)
            req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": pv, "type": otype, "price": price,
                   "sl": stop, "tp": ptp, "deviation": deviation, "magic": magic,
                   "comment": comment[:31] if len(parts) == 1 else f"{comment[:27]}-p{i}",
                   "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(symbol)}
            res = mt5.order_send(req)
            ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
            ok_all &= ok
            orders.append({"ok": ok, "volume": pv, "tp": ptp,
                           "error": None if ok else (res.comment if res else f"order_send failed: {mt5.last_error()}")})
        out = {"ok": ok_all, "volume": vol, "tickets": len(orders), "price": price, "sl": stop, "tp": tp, "orders": orders}
        if not ok_all:
            raise TradeRejected(next(o["error"] for o in orders if not o["ok"]) or "order rejected")
        return out

    def modify(self, ticket: int, sl: float, tp: float | None = None) -> dict:
        self._guard()
        pos = (mt5.positions_get(ticket=ticket) or [None])[0]
        if pos is None:
            raise TradeRejected("position not found")
        spec, tick = mt5.symbol_info(pos.symbol), mt5.symbol_info_tick(pos.symbol)
        digits, point = int(spec.digits or 5), float(spec.point or 0.0001)
        min_dist = max(int(spec.trade_stops_level or 0) * point, point)
        sl = min(sl, tick.bid - min_dist) if pos.type == mt5.POSITION_TYPE_BUY else max(sl, tick.ask + min_dist)
        tp = pos.tp if tp is None else tp
        res = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": pos.symbol,
                              "sl": round(sl, digits), "tp": round(tp, digits) if tp else 0.0})
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            raise TradeRejected(res.comment if res else f"modify failed: {mt5.last_error()}")
        return {"ok": True, "sl": round(sl, digits), "tp": tp}

    def close(self, ticket: int) -> dict:
        self._guard()
        pos = (mt5.positions_get(ticket=ticket) or [None])[0]
        if pos is None:
            raise TradeRejected("position not found")
        tick = mt5.symbol_info_tick(pos.symbol)
        buy = pos.type == mt5.POSITION_TYPE_BUY
        res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume,
                              "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "position": ticket,
                              "price": tick.bid if buy else tick.ask, "deviation": 20, "magic": pos.magic,
                              "comment": "MW-close", "type_time": mt5.ORDER_TIME_GTC,
                              "type_filling": self._filling(pos.symbol)})
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            raise TradeRejected(res.comment if res else f"close failed: {mt5.last_error()}")
        return {"ok": True}

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _filling(symbol: str) -> int:
        mode = int(getattr(mt5.symbol_info(symbol), "filling_mode", 0) or 0)
        return (mt5.ORDER_FILLING_FOK if mode & 1 else mt5.ORDER_FILLING_IOC if mode & 2
                else mt5.ORDER_FILLING_RETURN if mode & 4 else mt5.ORDER_FILLING_FOK)

    @staticmethod
    def _rebase(symbol: str, side: str, price: float, stop: float, tp: float, hint: float | None) -> tuple[float, float]:
        """Keep the signal's SL/TP distances but measure them from the live price (feeds disagree)."""
        spec = mt5.symbol_info(symbol)
        point = float(spec.point or 0.0001)
        min_dist = max(int(spec.trade_stops_level or 0) * point, point * 10)
        ref = hint if hint and hint > 0 else price
        risk = abs(ref - stop) if stop else 0.0
        reward = abs(tp - ref) if tp else 0.0
        risk = max(risk if risk > 0 else max(min_dist * 3, price * 0.002), min_dist * 1.2)
        reward = max(reward if reward > 0 else risk * 1.5, min_dist * 1.2)
        d = int(spec.digits or 5)
        if side == "buy":
            return round(price - risk, d), round(price + reward, d)
        return round(price + risk, d), round(price - reward, d)

    @staticmethod
    def _lot(symbol: str, stop: float, risk_pct: float, fixed: float | None, price: float, base: float) -> float:
        spec = mt5.symbol_info(symbol)
        if fixed and fixed > 0:
            vol = fixed
        else:
            info = mt5.account_info()
            money = (base if base > 0 else info.balance) * risk_pct / 100
            dist = abs(price - stop)
            if dist <= 0:
                raise TradeRejected("invalid stop distance")
            per_lot = dist / (spec.trade_tick_size or spec.point or 0.0001) * (spec.trade_tick_value or 1.0)
            vol = money / per_lot
        step, vmin, vmax = spec.volume_step or 0.01, spec.volume_min or 0.01, spec.volume_max or 100.0
        return round(max(vmin, min(vmax, math.floor(vol / step) * step)), 2)

    @staticmethod
    def _cap_margin(symbol: str, otype: int, vol: float, price: float, max_pct: float) -> float:
        info, spec = mt5.account_info(), mt5.symbol_info(symbol)
        budget = info.equity * max_pct / 100 - info.margin
        if budget <= 0:
            raise TradeRejected(f"margin budget exhausted ({max_pct:.0f}% of equity in use)")
        need = mt5.order_calc_margin(otype, symbol, vol, price)
        if not need or need <= budget:
            return vol
        step, vmin = spec.volume_step or 0.01, spec.volume_min or 0.01
        capped = math.floor(vol * budget / need / step) * step
        if capped < vmin:
            raise TradeRejected(f"margin budget too small for the min lot {vmin} on {symbol}")
        return round(capped, 2)

    @staticmethod
    def _split(symbol: str, vol: float, levels: list[dict]) -> list[tuple[float, float]]:
        """Split the volume over TP layers; fall back to 3 layers, then 1, when lots are too small."""
        spec = mt5.symbol_info(symbol)
        step, vmin = spec.volume_step or 0.01, spec.volume_min or 0.01
        wanted = [(float(lv.get("part") or 0), float(lv.get("r") or 0)) for lv in levels if float(lv.get("part") or 0) > 0]
        for plan in (wanted, [(40.0, 0.5), (30.0, 1.0), (30.0, 0.0)], [(100.0, 0.0)]):
            if not plan:
                continue
            total = sum(p for p, _ in plan)
            vols = [math.floor(p / total * vol / step) * step for p, _ in plan]
            if all(v >= vmin for v in vols):
                left = round(vol - sum(vols), 8)
                if left >= step:
                    vols[0] = round(vols[0] + math.floor(left / step) * step, 8)
                return [(round(v, 2), r) for v, (_, r) in zip(vols, plan)]
        return [(vol, 0.0)]
