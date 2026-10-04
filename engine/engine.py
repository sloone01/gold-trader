"""The trading engine: opens trades, runs the P1-P3 steps, trails, and reports closes.

The engine owns every trading decision. Callers (the demo script now, the API later)
only call open_trade / close_trade / modify_trade / reenter and tick() in a loop.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .broker import Broker, Position, Quote
from .models import Event, Side, StepStatus, StepState, StopMode, Trade, TradeSettings
from .store import MemoryStore
from .units import PIP, dollars, pips_to_price, price_to_pips, round_to_tick, split_volumes
from .validation import validate

_EPS = 1e-9


@dataclass
class RiskLimits:
    max_lot: float = 0.10
    max_open_trades: int = 3
    daily_loss_usd: float = 50.0     # block new trades after losing this much today (UTC)
    max_spread_pips: float = 10.0    # block new trades while the spread is wider than this


@dataclass
class EngineConfig:
    be_buffer_pips: float = 1.0      # "breakeven" means entry + this, to cover spread/commission
    backup_sl_pips: float = 300.0    # used in Far mode while no SL is set; None disables it
    max_attempts: int = 3
    retry_delay: float = 2.0         # seconds, multiplied by the attempt number
    close_history_wait: int = 40     # ticks to wait for closing deals to show up in history


class TradeError(Exception):
    pass


class Engine:
    def __init__(self, broker: Broker, store: MemoryStore, limits: RiskLimits | None = None,
                 config: EngineConfig | None = None, clock=time.time) -> None:
        self.broker = broker
        self.store = store
        self.limits = limits or RiskLimits()
        self.cfg = config or EngineConfig()
        self.clock = clock
        self.info = broker.symbol_info()
        self._trail_retry_at: dict[int, float] = {}
        self._history_waits: dict[int, int] = {}

    # ------------------------------------------------------------------ opening

    def check_can_open(self, settings: TradeSettings, quote: Quote | None) -> list[str]:
        errors, _ = validate(settings, self.info)
        if quote is None or not self.broker.market_open():
            errors.append("Market closed")
            return errors
        spread = price_to_pips(quote.spread)
        if spread > self.limits.max_spread_pips:
            errors.append(f"Spread is {spread:.0f} pips, above the {self.limits.max_spread_pips:.0f} pip limit")
        if settings.lot > self.limits.max_lot + _EPS:
            errors.append(f"Lot {settings.lot} is above your max lot {self.limits.max_lot}")
        if len(self.store.open_trades()) >= self.limits.max_open_trades:
            errors.append(f"Already {self.limits.max_open_trades} trades open (max)")
        lost = -self.today_result_usd()
        if lost >= self.limits.daily_loss_usd:
            errors.append(f"Daily loss limit reached (${lost:.2f} lost today)")
        return errors

    def open_trade(self, side: Side, settings: TradeSettings) -> tuple[Trade, list[Event]]:
        quote = self.broker.quote()
        errors = self.check_can_open(settings, quote)
        if errors:
            raise TradeError("; ".join(errors))

        ref = quote.ask if side is Side.BUY else quote.bid
        sl_pips = self._initial_sl_pips(settings)
        sl = self._price_at(ref, side, -sl_pips) if sl_pips else None
        tp = self._price_at(ref, side, settings.tp_pips) if settings.tp_pips else None

        r = self.broker.open(side, settings.lot, sl, tp, comment=f"gold-trader {settings.stop_mode.value}")
        if not r.ok:
            raise TradeError(f"Open rejected: {r.message}")

        # Distances are measured from the real fill, so re-place SL/TP if the fill slipped.
        entry = r.price
        events: list[Event] = []
        want_sl = self._price_at(entry, side, -sl_pips) if sl_pips else None
        want_tp = self._price_at(entry, side, settings.tp_pips) if settings.tp_pips else None
        if self._differs(want_sl, sl) or self._differs(want_tp, tp):
            m = self.broker.modify(r.ticket, want_sl, want_tp)
            if m.ok:
                sl, tp = want_sl, want_tp
            else:
                events.append(Event("error", r.ticket, f"Could not adjust SL/TP after slippage: {m.message}"))

        volumes = split_volumes(settings.lot, [s.close_pct for s in settings.steps],
                                self.info.vol_min, self.info.vol_step)
        trade = Trade(
            ticket=r.ticket, side=side, settings=settings, entry=entry, volume=settings.lot,
            sl=sl, tp=tp, steps=[StepState(planned_volume=v) for v in volumes],
            opened_at=self.clock(),
        )
        self.store.save(trade)

        sl_txt = f"SL {self._fmt(sl)}" if sl is not None else "no SL"
        if sl is not None and settings.stop_mode is StopMode.FAR and settings.sl_pips is None:
            sl_txt += " (backup)"
        events.insert(0, Event("opened", trade.ticket,
                               f"{side.value.capitalize()} {settings.lot} @ {self._fmt(entry)}, {sl_txt} "
                               f"({settings.stop_mode.value.capitalize()})", "View"))
        return trade, events

    def reenter(self, ticket: int) -> tuple[Trade, list[Event]]:
        old = self._get(ticket)
        if old.open:
            raise TradeError("Re-entry is only offered after a full close")
        return self.open_trade(old.side, old.settings)

    def preview(self, side: Side, settings: TradeSettings) -> dict:
        """Prices and dollar amounts the Trade screen shows before confirming."""
        q = self.broker.quote()
        ref = q.ask if side is Side.BUY else q.bid
        sl_pips = self._initial_sl_pips(settings)
        vols = split_volumes(settings.lot, [s.close_pct for s in settings.steps], self.info.vol_min, self.info.vol_step)
        errors, warnings = validate(settings, self.info)
        return {
            "entry": ref,
            "sl": {"price": self._price_at(ref, side, -sl_pips), "usd": -dollars(sl_pips, settings.lot)} if sl_pips else None,
            "tp": {"price": self._price_at(ref, side, settings.tp_pips),
                   "usd": dollars(settings.tp_pips, settings.lot)} if settings.tp_pips else None,
            "steps": [{"price": self._price_at(ref, side, s.pips), "close_volume": v,
                       "usd": dollars(s.pips, v)} for s, v in zip(settings.steps, vols)],
            "errors": errors, "warnings": warnings,
        }

    # ------------------------------------------------------------- manual edits

    def close_trade(self, ticket: int, volume: float | None = None) -> list[Event]:
        trade = self._get(ticket)
        pos = self.broker.position(ticket)
        if pos is None:
            return self._finalize(trade) or []
        vol = pos.volume if volume is None else min(volume, pos.volume)
        r = self.broker.close(ticket, trade.side, vol)
        if not r.ok:
            raise TradeError(f"Close rejected: {r.message}")
        return []  # the close event comes from tick() once MT5 reports it

    def modify_trade(self, ticket: int, sl_pips: float | None = None, tp_pips: float | None = None,
                     steps: dict[int, dict] | None = None, clear_tp: bool = False) -> list[Event]:
        """Edit SL / TP (in pips from entry; SL negative = loss side) or a step not yet hit."""
        trade = self._get(ticket)
        events = []
        new = trade.settings
        if steps:
            new_steps = list(new.steps)
            for idx, changes in steps.items():
                if trade.steps[idx].status is not StepStatus.PENDING:
                    raise TradeError(f"P{idx + 1} was already hit and can't be changed")
                new_steps[idx] = replace(new_steps[idx], **changes)
            new = replace(new, steps=new_steps)
        if tp_pips is not None or clear_tp:
            new = replace(new, tp_pips=None if clear_tp else tp_pips)
        errors, _ = validate(replace(new, stop_mode=StopMode.FAR, sl_pips=None), self.info)
        errors = [e for e in errors if not e.startswith("Lot")]
        if errors:
            raise TradeError("; ".join(errors))

        sl = trade.sl if sl_pips is None else self._price_at(trade.entry, trade.side, sl_pips)
        tp = None if new.tp_pips is None else self._price_at(trade.entry, trade.side, new.tp_pips)
        if sl != trade.sl or tp != trade.tp:
            q = self.broker.quote()
            for label, price, ok in (("SL", sl, self._stop_ok(trade.side, sl, q, is_tp=False)),
                                     ("TP", tp, self._stop_ok(trade.side, tp, q, is_tp=True))):
                if price is not None and not ok:
                    raise TradeError(f"{label} {self._fmt(price)} is too close to the current price")
            r = self.broker.modify(ticket, sl, tp)
            if not r.ok:
                raise TradeError(f"Modify rejected: {r.message}")
            if sl != trade.sl:
                events.append(Event("sl_moved", ticket, f"SL moved to {self._fmt(sl)}"))
            if tp != trade.tp:
                events.append(Event("sl_moved", ticket, f"TP moved to {self._fmt(tp) if tp else 'none'}"))
            trade.sl, trade.tp = sl, tp

        if steps:
            # Re-plan volumes for steps that haven't fired yet.
            vols = split_volumes(trade.volume, [s.close_pct for s in new.steps], self.info.vol_min, self.info.vol_step)
            for st, v in zip(trade.steps, vols):
                if st.status is StepStatus.PENDING:
                    st.planned_volume = v
        trade.settings = new
        self.store.save(trade)
        return events

    def retry_step(self, ticket: int, idx: int) -> None:
        """The Retry button on an error alert: put a failed step back in the queue."""
        st = self._get(ticket).steps[idx]
        if st.status is StepStatus.FAILED:
            st.status, st.attempts, st.next_attempt_at, st.error = StepStatus.PENDING, 0, 0.0, None
            self.store.save(self._get(ticket))

    # -------------------------------------------------------------- monitoring

    def tick(self) -> list[Event]:
        """One pass of the monitoring loop. Call every ~250 ms."""
        events: list[Event] = []
        trades = self.store.open_trades()
        if not trades:
            return events
        quote = self.broker.quote()
        for trade in trades:
            pos = self.broker.position(trade.ticket)
            if pos is None:
                events += self._finalize(trade) or []
                continue
            if quote is None:
                continue
            events += self._process(trade, pos, quote)
        return events

    def _process(self, trade: Trade, pos: Position, q: Quote) -> list[Event]:
        events: list[Event] = []
        # SL/TP can also be changed in MT5 directly; the broker's values win.
        if pos.sl != trade.sl or pos.tp != trade.tp:
            trade.sl, trade.tp = pos.sl, pos.tp
            self.store.save(trade)

        profit = self._profit_pips(trade, q)
        now = self.clock()
        for i, (cfg, st) in enumerate(zip(trade.settings.steps, trade.steps)):
            if st.status is not StepStatus.PENDING:
                continue
            if profit + _EPS < cfg.pips or now < st.next_attempt_at:
                break  # steps run strictly in order
            done, ev = self._run_step(trade, i, pos, q)
            events += ev
            if not done:
                break
            pos = self.broker.position(trade.ticket)
            if pos is None:
                return events  # last step closed everything; tick() finalizes next pass

        events += self._trail(trade, profit, q)
        return events

    def _run_step(self, trade: Trade, i: int, pos: Position, q: Quote) -> tuple[bool, list[Event]]:
        cfg, st = trade.settings.steps[i], trade.steps[i]
        label = f"P{i + 1}"
        parts = []

        if not st.close_done:
            vol = min(st.planned_volume, pos.volume)
            if vol + _EPS < self.info.vol_min:
                if st.planned_volume > 0:
                    parts.append("close skipped (below minimum lot)")
            else:
                r = self.broker.close(trade.ticket, trade.side, vol)
                if not r.ok:
                    return False, self._fail(trade, i, f"Partial close at {label}", r.message)
                st.closed_volume, st.close_price = vol, r.price
            st.close_done = True
            self.store.save(trade)
        if st.closed_volume:
            pips = (st.close_price - trade.entry) * trade.side.sign / PIP
            parts.insert(0, f"closed {st.closed_volume} at {pips:+.0f} pips ({self._usd(dollars(pips, st.closed_volume))})")

        fully_closed = st.closed_volume and st.closed_volume >= pos.volume - _EPS
        if not st.sl_done and cfg.move_sl_pips is not None and not fully_closed:
            new_sl = self._price_at(trade.entry, trade.side, self._lock_pips(cfg.move_sl_pips))
            if self._improves(trade, new_sl):
                if not self._stop_ok(trade.side, new_sl, q, is_tp=False):
                    return False, self._fail(trade, i, f"SL move at {label}", "stop too close to price")
                r = self.broker.modify(trade.ticket, new_sl, trade.tp)
                if not r.ok:
                    return False, self._fail(trade, i, f"SL move at {label}", r.message)
                trade.sl = new_sl
                parts.append("SL to breakeven" if cfg.move_sl_pips == 0 else f"SL to +{cfg.move_sl_pips:g}")
        st.sl_done = True
        st.status, st.error = StepStatus.HIT, None
        self.store.save(trade)
        return True, [Event("step_hit", trade.ticket, f"{label} hit: " + (", ".join(parts) or "no action"), "View")]

    def _trail(self, trade: Trade, profit: float, q: Quote) -> list[Event]:
        s = trade.settings
        if not s.trailing or trade.steps[2].status is not StepStatus.HIT:
            return []
        k = math.floor((profit - s.steps[2].pips) / s.trail_pips + _EPS)
        if k <= trade.trail_k or self.clock() < self._trail_retry_at.get(trade.ticket, 0):
            return []
        new_sl = self._price_at(trade.entry, trade.side, self._base_lock_pips(trade) + k * s.trail_pips)
        if not self._improves(trade, new_sl):
            trade.trail_k = k
            self.store.save(trade)
            return []
        if not self._stop_ok(trade.side, new_sl, q, is_tp=False):
            return []
        r = self.broker.modify(trade.ticket, new_sl, trade.tp)
        if not r.ok:
            self._trail_retry_at[trade.ticket] = self.clock() + self.cfg.retry_delay
            return []
        trade.sl, trade.trail_k = new_sl, k
        self.store.save(trade)
        locked = (new_sl - trade.entry) * trade.side.sign / PIP
        return [Event("sl_moved", trade.ticket, f"Trailing: SL moved to {self._fmt(new_sl)} ({locked:+.0f} pips)")]

    def _fail(self, trade: Trade, i: int, what: str, message: str) -> list[Event]:
        st = trade.steps[i]
        st.attempts += 1
        st.error = message
        if st.attempts >= self.cfg.max_attempts:
            st.status = StepStatus.FAILED
            self.store.save(trade)
            return [Event("error", trade.ticket, f"{what} rejected: {message}", "Retry")]
        st.next_attempt_at = self.clock() + self.cfg.retry_delay * st.attempts
        self.store.save(trade)
        return []

    def _finalize(self, trade: Trade) -> list[Event] | None:
        deals = self.broker.closing_deals(trade.ticket)
        bot_closed = sum(st.closed_volume for st in trade.steps)
        final = [d for d in deals if d.reason != "bot"] or deals
        closed_vol = sum(d.volume for d in deals)
        if closed_vol + _EPS < trade.volume:
            # History can lag a moment behind the position disappearing.
            n = self._history_waits.get(trade.ticket, 0) + 1
            self._history_waits[trade.ticket] = n
            if n < self.cfg.close_history_wait:
                return None
        self._history_waits.pop(trade.ticket, None)
        self._trail_retry_at.pop(trade.ticket, None)

        usd = round(sum(d.profit for d in deals), 2)
        pips = (sum((d.price - trade.entry) * trade.side.sign / PIP * d.volume for d in deals) / closed_vol
                if closed_vol else 0.0)
        reason = final[-1].reason if final else "unknown"
        if reason == "bot" and bot_closed + _EPS >= trade.volume:
            reason = "steps"
        trade.open = False
        trade.closed_at = self.clock()
        trade.result_pips, trade.result_usd, trade.close_reason = round(pips, 1), usd, reason
        self.store.save(trade)
        by = {"sl": "Closed by SL", "tp": "Closed by TP", "manual": "Closed manually",
              "steps": "Closed by profit steps"}.get(reason, "Closed")
        last_pips = (final[-1].price - trade.entry) * trade.side.sign / PIP if final else pips
        return [Event("closed", trade.ticket, f"{by} at {last_pips:+.0f} pips. Total {self._usd(usd)}", "Re-enter")]

    # ------------------------------------------------------------------ helpers

    def today_result_usd(self) -> float:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return sum(t.result_usd or 0 for t in self.store.closed_trades() if (t.closed_at or 0) >= start)

    def _initial_sl_pips(self, s: TradeSettings) -> float | None:
        sl = s.effective_sl_pips()
        if sl is None and s.stop_mode is StopMode.FAR:
            return self.cfg.backup_sl_pips
        return sl

    def _lock_pips(self, move_pips: float) -> float:
        return self.cfg.be_buffer_pips if move_pips == 0 else move_pips

    def _base_lock_pips(self, trade: Trade) -> float:
        """Highest profit level locked by the steps; trailing builds on it."""
        locks = [self._lock_pips(c.move_sl_pips) for c, st in zip(trade.settings.steps, trade.steps)
                 if st.status is StepStatus.HIT and c.move_sl_pips is not None]
        if locks:
            return max(locks)
        return trade.settings.steps[2].pips - trade.settings.trail_pips

    def _profit_pips(self, trade: Trade, q: Quote) -> float:
        price = q.bid if trade.side is Side.BUY else q.ask   # the price the trade would close at
        return (price - trade.entry) * trade.side.sign / PIP

    def _price_at(self, ref: float, side: Side, pips: float) -> float:
        return round_to_tick(ref + side.sign * pips_to_price(pips, self.info.tick_size), self.info.tick_size)

    def _improves(self, trade: Trade, new_sl: float) -> bool:
        return trade.sl is None or (new_sl - trade.sl) * trade.side.sign > self.info.tick_size / 2

    def _stop_ok(self, side: Side, price: float | None, q: Quote, is_tp: bool) -> bool:
        if price is None:
            return True
        gap = max(self.info.stops_level, self.info.freeze_level)
        close_price = q.bid if side is Side.BUY else q.ask
        dist = (price - close_price) * side.sign
        return dist >= gap - _EPS if is_tp else -dist >= gap - _EPS

    def _differs(self, a: float | None, b: float | None) -> bool:
        if a is None or b is None:
            return a is not b
        return abs(a - b) >= self.info.tick_size / 2

    def _get(self, ticket: int) -> Trade:
        t = self.store.trades.get(ticket)
        if t is None:
            raise TradeError(f"Unknown trade {ticket}")
        return t

    def _fmt(self, price: float | None) -> str:
        return "none" if price is None else f"{price:.{self.info.digits}f}"

    @staticmethod
    def _usd(v: float) -> str:
        return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}".replace(".00", "")
