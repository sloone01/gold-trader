"""Runs the engine in a background thread and exposes thread-safe calls for the API.

Everything that touches the engine or the broker goes through `self.lock`, so the
monitoring loop and an API request never interleave half-way through a step.
"""
from __future__ import annotations

import logging
import queue
import random
import threading
import time
from dataclasses import asdict, replace

from engine import Engine, EngineConfig, Event, Preset, RiskLimits, Side, StopMode, Trade, TradeSettings
from engine.broker import Broker, Quote
from engine.store import MemoryStore
from engine.units import PIP, dollars, price_to_pips

log = logging.getLogger("gold-trader")

DEFAULT_PRESETS = [
    ("Default hybrid", TradeSettings()),
    ("Tight scalp", TradeSettings(stop_mode=StopMode.TIGHT, trail_pips=30)),
    ("Far runner", TradeSettings(stop_mode=StopMode.FAR, sl_pips=150, trailing=True, trail_pips=50)),
]


class TradingService:
    def __init__(self, broker: Broker, store: MemoryStore, limits: RiskLimits | None = None,
                 config: EngineConfig | None = None, tick_interval: float = 0.25,
                 sim_walk: bool = False) -> None:
        saved = store.get_setting("limits")
        if saved:  # ignore keys from older versions (e.g. the removed max_open_trades)
            saved = {k: v for k, v in saved.items() if k in RiskLimits.__dataclass_fields__}
        limits = RiskLimits(**saved) if saved else (limits or RiskLimits())
        self.engine = Engine(broker, store, limits, config)
        self.broker = broker
        self.store = store
        self.lock = threading.RLock()
        self.tick_interval = tick_interval
        self.sim_walk = sim_walk
        self._subs: list[queue.Queue] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_tick: float = 0.0
        self.last_error: str | None = None
        if not store.list_presets():
            for name, settings in DEFAULT_PRESETS:
                settings = TradeSettings.from_dict(asdict(settings))
                store.save_preset(Preset(name, settings))

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="engine-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as ex:  # keep the loop alive; broker-side SL still protects the trade
                self.last_error = str(ex)
                log.exception("tick failed")
                time.sleep(2)
            self._stop.wait(self.tick_interval)

    def tick(self) -> list[Event]:
        with self.lock:
            if self.sim_walk and hasattr(self.broker, "set_price"):
                self.broker.set_price(self.broker.bid + random.gauss(0, 0.12))
            events = self.engine.tick()
            self.last_tick = time.time()
            self.last_error = None
        for e in events:
            self.publish(e)
        return events

    # --------------------------------------------------------------- events

    def publish(self, event: Event | dict) -> dict:
        data = asdict(event) if isinstance(event, Event) else dict(event)
        data = self.store.add_event(data)
        for q in list(self._subs):
            try:
                q.put_nowait(data)
            except queue.Full:
                pass
        return data

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    # ------------------------------------------------------------ snapshots

    def status(self) -> dict:
        with self.lock:
            quote = self.broker.quote()
            acc = self.broker.account()
            open_trades = [self.trade_view(t, quote) for t in self.store.open_trades()]
            return {
                "symbol": getattr(self.broker, "symbol", "XAUUSD"),
                "connected": acc.connected,
                "market_open": self.broker.market_open(),
                "quote": self._quote_view(quote),
                "account": asdict(acc),
                "today_usd": round(self.engine.today_result_usd(), 2),
                "floating_usd": round(sum(t["profit_usd"] or 0 for t in open_trades), 2),
                "open_trades": open_trades,
                "unmanaged": [{"ticket": p.ticket, "side": p.side.value, "volume": p.volume, "entry": p.price_open,
                               "sl": p.sl, "tp": p.tp} for p in self.engine.unmanaged_positions()],
                "limits": asdict(self.engine.limits),
                "info": asdict(self.engine.info),
                "engine": {"last_tick": self.last_tick, "last_error": self.last_error,
                           "running": self._thread is not None, "sim": self.sim_walk},
            }

    def _quote_view(self, q: Quote | None) -> dict | None:
        if q is None:
            return None
        return {"bid": q.bid, "ask": q.ask, "spread_pips": price_to_pips(q.spread), "time": q.time}

    def trade_view(self, t: Trade, quote: Quote | None = None) -> dict:
        d = t.to_dict()
        d["settings"] = self._settings_view(t.settings)
        info = self.engine.info
        sign = t.side.sign
        pos = self.broker.position(t.ticket) if t.open else None
        cur_vol = pos.volume if pos else (0.0 if not t.open else t.volume)
        d["current_volume"] = cur_vol
        d["sl_pips"] = round((t.sl - t.entry) * sign / PIP, 1) if t.sl is not None else None
        d["tp_pips"] = round((t.tp - t.entry) * sign / PIP, 1) if t.tp is not None else None
        d["step_prices"] = [round(t.entry + sign * s.pips * PIP, info.digits) for s in t.settings.steps]
        if t.open and quote is not None:
            price = quote.bid if t.side is Side.BUY else quote.ask
            pips = round((price - t.entry) * sign / PIP, 1)
            d["profit_pips"] = pips
            realized = sum(dl.profit for dl in self.broker.closing_deals(t.ticket))  # steps + manual partials
            d["realized_usd"] = round(realized, 2)
            d["profit_usd"] = round(dollars(pips, cur_vol, info.contract_size) + realized, 2)
            d["current_price"] = price
        else:
            d["profit_pips"] = t.result_pips
            d["profit_usd"] = t.result_usd
            d["current_price"] = None
        d["digits"] = info.digits
        return d

    @staticmethod
    def _settings_view(s: TradeSettings) -> dict:
        d = asdict(s)
        d["stop_mode"] = s.stop_mode.value
        return d

    def preset_view(self, p: Preset) -> dict:
        return {"id": p.id, "name": p.name, "settings": self._settings_view(p.settings)}

    # --------------------------------------------------------------- actions

    def preview(self, side: Side, settings: TradeSettings) -> dict:
        with self.lock:
            out = self.engine.preview(side, settings)
            out["errors"] = self.engine.check_can_open(settings, self.broker.quote()) or out["errors"]
            return out

    def open_trade(self, side: Side, settings: TradeSettings) -> dict:
        with self.lock:
            trade, events = self.engine.open_trade(side, settings)
            view = self.trade_view(trade, self.broker.quote())
        for e in events:
            self.publish(e)
        return view

    def close_trade(self, ticket: int, volume: float | None) -> list[dict]:
        with self.lock:
            events = self.engine.close_trade(ticket, volume)
            if volume is not None and not events:
                events = [Event("info", ticket, f"Closed {volume} lot manually")]
        return [self.publish(e) for e in events]

    def modify_trade(self, ticket: int, sl_pips=None, tp_pips=None, steps=None, clear_tp=False) -> dict:
        with self.lock:
            events = self.engine.modify_trade(ticket, sl_pips=sl_pips, tp_pips=tp_pips, steps=steps, clear_tp=clear_tp)
            if steps and not events:
                events = [Event("info", ticket, "Steps updated")]
            view = self.trade_view(self.engine._get(ticket), self.broker.quote())
        for e in events:
            self.publish(e)
        return view

    def retry_step(self, ticket: int, idx: int) -> dict:
        with self.lock:
            self.engine.retry_step(ticket, idx)
            return self.trade_view(self.engine._get(ticket), self.broker.quote())

    def adopt(self, ticket: int, settings: TradeSettings) -> dict:
        with self.lock:
            trade, events = self.engine.adopt(ticket, settings)
            view = self.trade_view(trade, self.broker.quote())
        for e in events:
            self.publish(e)
        return view

    def unmanaged(self) -> list[dict]:
        with self.lock:
            return [{"ticket": p.ticket, "side": p.side.value, "volume": p.volume, "entry": p.price_open,
                     "sl": p.sl, "tp": p.tp} for p in self.engine.unmanaged_positions()]

    def reenter(self, ticket: int) -> dict:
        with self.lock:
            trade, events = self.engine.reenter(ticket)
            view = self.trade_view(trade, self.broker.quote())
        for e in events:
            self.publish(e)
        return view

    def set_limits(self, limits: RiskLimits) -> RiskLimits:
        with self.lock:
            self.engine.limits = limits
            self.store.put_setting("limits", asdict(limits))
        return limits

    def get_trade(self, ticket: int) -> dict:
        with self.lock:
            return self.trade_view(self.engine._get(ticket), self.broker.quote())

    def history(self, limit: int = 100) -> list[dict]:
        with self.lock:
            trades = sorted(self.store.closed_trades(), key=lambda t: t.closed_at or 0, reverse=True)[:limit]
            return [self.trade_view(t) for t in trades]
