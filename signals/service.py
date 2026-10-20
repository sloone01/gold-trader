"""Receives Telegram messages and feed signals, plans trades by channel trust, and places or queues them.

Beyond the plain "open at market" path this handles:
- waiting for the stop: a signal without SL waits for the channel's "SL ..." message (wait_for_sl_s)
- entry ladders and pending orders: legs that fill when price reaches them (checked in tick())
- follow-ups matched by reply first, then to the channel's latest signal, applied to every leg
- breakeven armor: a "TP1 hit" from the channel moves its trades' stops to breakeven
"""
from __future__ import annotations

import logging
import threading
import time
import zlib

from engine import Event, Side, TradeError
from engine.units import PIP

from .mapper import build_plan, plan_legs, settings_from_dict
from .models import MAX_WEIGHT, Channel, ParsedSignal, Signal, SignalSettings, feed_channel_id, is_feed
from .parser import Parser, normalise

log = logging.getLogger("gold-trader.signals")
OWN_FEED = "me"          # signals you type in the app ("My signals" channel)


class SignalService:
    def __init__(self, trading, parser: Parser | None) -> None:
        self.trading = trading          # server.service.TradingService
        self.store = trading.store
        self.parser = parser            # None without ANTHROPIC_API_KEY: feeds still work, Telegram text can't be read
        self.lock = threading.RLock()
        saved = self.store.get_setting("signals") or {}
        self.cfg = SignalSettings(**{k: v for k, v in saved.items() if k in SignalSettings.__dataclass_fields__})
        self.telegram = None            # set by the server when Telegram is configured
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Background checks: ladder legs reaching their price, stop waits running out."""
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="signals", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("signal tick failed")
            self._stop.wait(1.0)

    # ------------------------------------------------------------- channels

    def channels(self) -> list[Channel]:
        return [Channel.from_dict(c) for c in self.store.channels.values()]

    def channel(self, channel_id: int) -> Channel | None:
        c = self.store.channels.get(int(channel_id))
        return Channel.from_dict(c) if c else None

    def save_channel(self, ch: Channel) -> Channel:
        ch.weight = max(1, min(int(ch.weight), MAX_WEIGHT))
        if ch.mode not in ("off", "confirm", "auto"):
            raise ValueError("mode must be off, confirm or auto")
        self.store.save_channel(ch.to_dict())
        if self.telegram:
            self.telegram.set_channels(self.listened_ids())
        return ch

    def listened_ids(self) -> list[int]:
        """Telegram ids to listen to (feeds arrive over the API instead)."""
        return [c.id for c in self.channels() if c.mode != "off" and not is_feed(c.id)]

    def delete_channel(self, channel_id: int) -> bool:
        ok = self.store.delete_channel(int(channel_id))
        if self.telegram:
            self.telegram.set_channels(self.listened_ids())
        return ok

    def set_settings(self, cfg: SignalSettings) -> SignalSettings:
        if cfg.sizing not in ("weight", "risk"):
            raise ValueError("sizing must be weight or risk")
        if cfg.no_sl_action not in ("default", "skip"):
            raise ValueError("no_sl_action must be default or skip")
        self.cfg = cfg
        self.store.put_setting("signals", cfg.to_dict())
        return cfg

    def channel_review(self, channel_id: int) -> dict:
        """Scorecard: how this channel's signals have actually performed with our rules."""
        sigs = [s for s in self.store.recent_signals(1000, channel_id)]
        opens = [s for s in sigs if (s.get("parsed") or {}).get("action") == "open"]
        placed = [s for s in opens if _tickets(s)]
        tickets = {t for s in placed for t in _tickets(s)}
        trades = [t for t in self.store.trades.values() if t.ticket in tickets]
        closed = [t for t in trades if not t.open]
        wins = [t for t in closed if (t.result_usd or 0) > 0]
        usd = round(sum(t.result_usd or 0 for t in closed), 2)
        pips = round(sum(t.result_pips or 0 for t in closed), 1)
        win_rate = round(100 * len(wins) / len(closed)) if closed else None
        # What the channel itself reports about its calls (counted even for trades we didn't take).
        claims = [s.get("parsed") or {} for s in sigs if s["status"] == "result"]
        claimed_tp = sum(1 for c in claims if c.get("result") == "tp")
        claimed_sl = sum(1 for c in claims if c.get("result") == "sl")
        claimed_pips = round(sum(c.get("result_pips") or 0 for c in claims), 0)
        claimed_rate = round(100 * claimed_tp / (claimed_tp + claimed_sl)) if claimed_tp + claimed_sl else None
        suggestion = None
        if len(closed) >= 10:
            if win_rate >= 60 and usd > 0:
                suggestion = "Consider raising the weight: 10+ closed trades, profitable, win rate above 60%."
            elif win_rate < 40 or usd < 0:
                suggestion = "Consider lowering the weight: this channel is losing with our rules."
        return {
            "channel_id": channel_id, "signals": len(sigs), "open_signals": len(opens),
            "ignored": sum(1 for s in sigs if s["status"] == "ignored"),
            "placed": len(placed), "open_now": len(trades) - len(closed), "closed": len(closed),
            "wins": len(wins), "losses": len(closed) - len(wins), "win_rate": win_rate,
            "result_usd": usd, "result_pips": pips,
            "avg_pips": round(pips / len(closed), 1) if closed else None,
            "last_signal_at": sigs[-1]["ts"] if sigs else None,
            "claimed_tp": claimed_tp, "claimed_sl": claimed_sl, "claimed_pips": claimed_pips,
            "claimed_rate": claimed_rate, "claims": len(claims),
            "suggestion": suggestion,
        }

    # --------------------------------------------------------------- intake

    def handle_message(self, channel_id: int, channel_title: str, msg_id: int, text: str,
                       ts: float | None = None, reply_to: int | None = None,
                       topic_id: int | None = None, image: bytes | None = None,
                       image_type: str = "image/jpeg") -> Signal | None:
        ch = self.channel(channel_id)
        if ch is None or ch.mode == "off" or (not (text or "").strip() and not image):
            return None
        if ch.topics and topic_id not in ch.topic_ids():
            return None                                   # another topic of the same group
        if ch.topics:
            channel_title = f"{channel_title or ch.title} › {ch.topic_title(topic_id)}"
        shown = (text or "").strip() or "(photo)"
        if image and text:
            shown = "[photo] " + shown
        sig = Signal(None, channel_id, channel_title or ch.title, msg_id, shown, ts or time.time(), reply_to=reply_to)
        if self.parser is None:
            sig.status, sig.error = "failed", "No AI parser: set ANTHROPIC_API_KEY to read channel messages"
            return self._save(sig)
        try:
            context = self._reply_context(channel_id, reply_to)
            parsed = normalise(self.parser(text or "", context, image=image, image_type=image_type) if image
                               else self.parser(text, context))
        except Exception as ex:  # parser outage must not kill the listener
            log.exception("parse failed")
            sig.status, sig.error = "failed", f"Parse failed: {ex}"
            return self._save(sig)
        with self.lock:
            return self._route(sig, ch, parsed)

    def own_signal(self, text: str) -> Signal | None:
        """A signal you type in the app: handled as the "My signals" channel (created in confirm mode)."""
        cid = feed_channel_id(OWN_FEED)
        if self.channel(cid) is None:
            self.save_channel(Channel(cid, "My signals", weight=3, mode="confirm"))
        return self.handle_message(cid, "My signals", int(time.time() * 1000) % 2_000_000_000, text)

    def _route(self, sig: Signal, ch: Channel, parsed: ParsedSignal) -> Signal:
        sig.parsed = parsed.to_dict()
        if parsed.action == "ignore":
            sig.status, sig.error = "ignored", parsed.reason
            return self._save(sig)
        if parsed.action == "result":
            sig.status, sig.error = "result", parsed.reason
            if parsed.result == "tp":
                self._armor(sig, ch)
            return self._save(sig)
        if parsed.action == "watch":
            sig.status, sig.error = "watch", parsed.reason
            self._save(sig)
            self.trading.publish(Event("signal", 0, f"{sig.channel_title}: setup to watch - {parsed.reason}", "Signals"))
            return sig
        if parsed.action == "open":
            if parsed.sl is not None and self._complete_waiting(sig, ch, parsed.sl, parsed.side):
                return sig
            return self._handle_open(sig, ch, parsed)
        if parsed.action == "move_sl" and parsed.move_sl_to and not parsed.move_sl_to.lower().startswith("break"):
            try:
                price = float(parsed.move_sl_to)
            except ValueError:
                price = None
            if price is not None and self._complete_waiting(sig, ch, price, None):
                return sig
        return self._handle_followup(sig, ch, parsed)

    def ingest(self, feed: str, msg_key: str, action: str, side: str | None = None,
               entry: float | None = None, sl: float | None = None, tps: list[float] | None = None,
               close_pct: float | None = None, move_sl_to: str | None = None, note: str = "",
               title: str | None = None, confidence: float = 1.0, reanchor: bool = False) -> Signal | None:
        """A structured signal pushed by another app. The feed is a channel like any Telegram one
        (trust weight, mode, preset, scorecard); it is created in confirm mode on first use.

        msg_key identifies the message: sending the same key again returns the stored signal
        instead of trading twice, so a sender may safely retry.

        reanchor: the sender's prices come from another feed (e.g. futures vs the broker's spot), so
        keep the distances from its entry and shift every level onto our current price."""
        cid = feed_channel_id(feed)
        with self.lock:
            ch = self.channel(cid) or self.save_channel(Channel(cid, title or feed, weight=3, mode="confirm"))
            if ch.mode == "off":
                return None
            msg_id = zlib.crc32(msg_key.encode())
            for s in self.store.recent_signals(500, cid):
                if s["msg_id"] == msg_id:
                    return _sig(s)
            shift = 0.0
            if reanchor and action == "open" and entry is not None:
                q = self.trading.broker.quote()
                if q is not None:
                    shift = round((q.bid + q.ask) / 2 - entry, 2)
                    entry = entry + shift
                    sl = sl + shift if sl is not None else None
                    tps = [tp + shift for tp in tps or []]
            p = normalise(ParsedSignal(action=action, symbol="XAUUSD", side=side, entry=entry, sl=sl,
                                       tps=list(tps or []), close_pct=close_pct, move_sl_to=move_sl_to,
                                       confidence=confidence, reason=note))
            sig = Signal(None, cid, ch.title, msg_id, note or _describe(p), time.time())
            if p.action == "open":
                held = self._holding(cid, p.side)
                if held:
                    sig.parsed, sig.status, sig.error = p.to_dict(), "ignored", held
                    return self._save(sig)
            elif p.action == "close":
                self._withdraw_pending(cid)
            sig = self._route(sig, ch, p)
            if shift and sig.plan is not None and "warnings" in sig.plan:
                sig.plan["warnings"].append(f"Levels shifted {shift / PIP:+.0f} pips onto the broker price")
                self._save(sig)
            return sig

    def _holding(self, channel_id: int, side: str) -> str | None:
        """A feed repeats its idea while it stays valid: one trade (or pending signal) per side is enough."""
        for s in self.store.recent_signals(200, channel_id):
            if (s.get("parsed") or {}).get("side") != side or (s.get("parsed") or {}).get("action") != "open":
                continue
            if s["status"] in ("pending", "waiting_sl", "armed"):
                return f"Already waiting on signal #{s['id']} for this {side}"
            for t in self._open_trades_of(s):
                return f"Already holding #{t.ticket} for this {side}"
        return None

    def _withdraw_pending(self, channel_id: int) -> None:
        """The feed closed its idea: a trade still waiting for a tap is no longer valid."""
        for s in self.store.recent_signals(200, channel_id):
            if s["status"] == "pending" and (s.get("parsed") or {}).get("action") == "open":
                sig = _sig(s)
                sig.status, sig.error = "dismissed", "Withdrawn by the feed before it was placed"
                self._save(sig)

    def _reply_context(self, channel_id: int, reply_to: int | None) -> str:
        """What the parser needs besides the message: the current price (to expand abbreviated
        levels), the channel's last few messages (split signals) and the replied-to message."""
        parts = []
        q = self.trading.broker.quote()
        if q:
            parts.append(f"Current gold (XAUUSD) price: {q.bid:.2f}")
        recent = self.store.recent_signals(6, channel_id)
        if recent:
            parts.append("Previous messages in this channel (oldest first):\n" +
                         "\n".join(f"- {s['text'][:160]}" for s in recent))
        if reply_to:
            for s in reversed(self.store.recent_signals(200, channel_id)):
                if s["msg_id"] == reply_to:
                    parts.append(f"The new message replies to:\n{s['text'][:300]}")
                    break
        return "\n\n".join(parts)

    # ----------------------------------------------------------------- opens

    def _handle_open(self, sig: Signal, ch: Channel, p: ParsedSignal, waited: bool = False) -> Signal:
        if p.sl is None and not waited and self.cfg.wait_for_sl_s > 0:
            sig.status = "waiting_sl"
            sig.plan = {"wait_until": time.time() + self.cfg.wait_for_sl_s}
            sig.note = f"No stop in the signal: waiting up to {self.cfg.wait_for_sl_s:g} s for the channel's SL"
            self._save(sig)
            self.trading.publish(Event("signal", 0, f"{sig.channel_title}: {p.side} without SL, waiting for the stop", "Signals"))
            return sig
        with self.trading.lock:
            quote = self.trading.broker.quote()
            if quote is None:
                sig.status, sig.error = "failed", "No quote (market closed?)"
                return self._save(sig)
            plan = self._plan(p, ch, quote)
            sig.plan = plan
            if plan["errors"]:
                sig.status, sig.error = "failed", "; ".join(plan["errors"])
                return self._save(sig)
            if self._auto(ch):
                return self._arm(sig, ch)
        sig.status = "pending"
        self._save(sig)
        legs = plan["legs"]
        what = f"{len(legs)} entries" if len(legs) > 1 else ("at " + f"{legs[0]['price']:g}" if legs[0]["kind"] != "market" else "now")
        self.trading.publish(Event("signal", 0, f"{sig.channel_title}: {p.side} {plan['lot']} lot {what}, "
                                                f"SL {p.sl if p.sl is not None else 'default'} - tap to place", "Signals"))
        return sig

    def _plan(self, p: ParsedSignal, ch: Channel, quote) -> dict:
        acc = self.trading.broker.account()
        plan = build_plan(p, ch, self._preset(ch), self.cfg, quote, self.trading.engine.info,
                          balance=acc.balance if acc else None, max_lot=self.trading.engine.limits.max_lot)
        plan["legs"] = [{**leg, "status": "waiting", "ticket": None} for leg in
                        plan_legs(p, self.cfg, plan["lot"], self.trading.engine.info)]
        if len(plan["legs"]) > 1 or plan["legs"][0]["kind"] != "market" or plan["legs"][0]["price"] is not None:
            plan["expires_at"] = time.time() + self.cfg.ladder_expiry_min * 60
        return plan

    def _arm(self, sig: Signal, ch: Channel) -> Signal:
        """Start the entries: legs at market fill now, the rest wait for their price in tick()."""
        sig.status = "armed"
        self._fill_legs(sig, ch)
        return self._save(sig)

    def _fill_legs(self, sig: Signal, ch: Channel) -> None:
        plan, p = sig.plan, ParsedSignal(**sig.parsed)
        q = self.trading.broker.quote()
        if q is None:
            return
        side = Side(p.side)
        price = q.ask if side is Side.BUY else q.bid
        drift = self.cfg.max_entry_drift_pips * PIP
        sl_hit = p.sl is not None and (price - p.sl) * side.sign <= 0
        for leg in plan["legs"]:
            if leg["status"] != "waiting":
                continue
            if sl_hit:
                leg["status"] = "expired"
                continue
            lv = leg["price"]
            if leg["kind"] == "market":
                go = lv is None or (lv - price) * side.sign >= -drift      # at or better than the zone edge
            elif leg["kind"] == "limit":
                go = (lv - price) * side.sign >= 0
            else:                                                        # stop: breakout through the level
                go = (price - lv) * side.sign >= 0
            if not go:
                continue
            try:
                lp = build_plan(p, ch, self._preset(ch), self.cfg, q, self.trading.engine.info, lot=leg["lot"])
                if lp["errors"]:
                    raise TradeError("; ".join(lp["errors"]))
                view = self.trading.open_trade(side, settings_from_dict(lp["settings"]))
                leg.update(status="placed", ticket=view["ticket"], filled=view["entry"])
                sig.tickets.append(view["ticket"])
                sig.ticket = sig.ticket or view["ticket"]
            except TradeError as ex:
                leg.update(status="failed", error=str(ex))
        legs = plan["legs"]
        if sig.tickets:
            sig.status = "placed"
        if all(leg["status"] != "waiting" for leg in legs):
            plan.pop("expires_at", None)
            if not sig.tickets:
                sig.status = "failed" if any(leg["status"] == "failed" for leg in legs) else "expired"
                sig.error = next((leg.get("error") for leg in legs if leg.get("error")), None) or \
                    ("Stop was hit before any entry filled" if sl_hit else sig.error)
        waiting = sum(1 for leg in legs if leg["status"] == "waiting")
        if waiting and sig.tickets:
            sig.note = f"{len(sig.tickets)} of {len(legs)} entries filled, {waiting} waiting"
        elif len(legs) > 1 and not waiting:
            sig.note = f"{len(sig.tickets)} of {len(legs)} entries filled"

    def _complete_waiting(self, sig: Signal, ch: Channel, sl: float, side: str | None) -> bool:
        """The stop for a signal that came without one: finish that signal instead of starting a new one."""
        for s in reversed(self.store.recent_signals(50, ch.id)):
            if s["status"] != "waiting_sl" or (side and (s.get("parsed") or {}).get("side") != side):
                continue
            waiting = _sig(s)
            p = ParsedSignal(**waiting.parsed)
            p.sl = sl
            waiting.parsed = p.to_dict()
            waiting.note = f"Stop {sl:g} from message #{sig.msg_id}"
            self._handle_open(waiting, ch, p, waited=True)
            sig.parsed = sig.parsed or {}
            sig.status, sig.note = "applied", f"Completed signal #{waiting.id} with SL {sl:g}"
            sig.ticket = waiting.ticket
            self._save(sig)
            return True
        return False

    # ------------------------------------------------------------- follow-ups

    def _targets(self, ch: Channel, reply_to: int | None) -> tuple[Signal | None, list]:
        """(the signal a follow-up is about, its open trades). A reply names it; otherwise the
        channel's latest signal that has open trades or entries still waiting."""
        recent = list(reversed(self.store.recent_signals(300, ch.id)))
        if reply_to:
            for s in recent:
                if s["msg_id"] != reply_to:
                    continue
                if (s.get("parsed") or {}).get("action") == "open":
                    return _sig(s), self._open_trades_of(s)
                tickets = (s.get("plan") or {}).get("tickets") or _tickets(s)   # replied to a follow-up
                origin = next((o for o in recent if (o.get("parsed") or {}).get("action") == "open"
                               and set(_tickets(o)) & set(tickets)), None)
                if origin:
                    return _sig(origin), self._open_trades_of(origin)
                break
        for s in recent:
            if (s.get("parsed") or {}).get("action") != "open":
                continue
            trades = self._open_trades_of(s)
            if trades or s["status"] == "armed":
                return _sig(s), trades
        return None, []

    def _open_trades_of(self, s: dict) -> list:
        out = []
        for t in _tickets(s):
            tr = self.store.trades.get(t)
            if tr and tr.open:
                out.append(tr)
        return out

    def _handle_followup(self, sig: Signal, ch: Channel, p: ParsedSignal) -> Signal:
        origin, trades = self._targets(ch, sig.reply_to)
        waiting_legs = origin is not None and any(l["status"] == "waiting" for l in (origin.plan or {}).get("legs", []))
        if not trades and not waiting_legs:
            sig.status, sig.error = "ignored", f"{p.action}: no open trade from this channel"
            return self._save(sig)
        plan = {"action": p.action, "tickets": [t.ticket for t in trades], "origin": origin.id if origin else None}
        if p.action == "close_partial":
            plan["close_pct"] = p.close_pct or 50.0
        elif p.action == "move_sl":
            if (p.move_sl_to or "").lower().startswith("break") or not p.move_sl_to:
                plan["sl"] = "breakeven"
            else:
                try:
                    plan["sl"] = float(p.move_sl_to)
                except ValueError:
                    sig.status, sig.error = "failed", f"Can't read SL price '{p.move_sl_to}'"
                    return self._save(sig)
        elif p.action == "modify_tp":
            if not p.tps:
                sig.status, sig.error = "failed", "No TP price in the message"
                return self._save(sig)
            plan["tp"] = p.tps[-1]
        sig.plan, sig.ticket = plan, (plan["tickets"] or [None])[0]
        if self._auto(ch):
            return self._apply(sig)
        sig.status = "pending"
        self._save(sig)
        self.trading.publish(Event("signal", sig.ticket or 0, f"{sig.channel_title}: {p.action.replace('_', ' ')} on "
                                   f"{_list_tickets(plan['tickets']) or 'waiting entries'} - tap to apply", "Signals"))
        return sig

    def _apply(self, sig: Signal) -> Signal:
        plan = sig.plan or {}
        errors, done = [], []
        tickets = plan.get("tickets") or ([plan["ticket"]] if plan.get("ticket") else [])
        for ticket in tickets:
            try:
                self._apply_one(plan, ticket)
                done.append(ticket)
            except TradeError as ex:
                errors.append(f"#{ticket}: {ex}" if len(tickets) > 1 else str(ex))
        self._update_waiting_legs(plan)
        if errors and not done:
            sig.status, sig.error = "failed", "; ".join(errors)
        else:
            sig.status = "applied"
            sig.error = "; ".join(errors) or None
        return self._save(sig)

    def _apply_one(self, plan: dict, ticket: int) -> None:
        t = self.trading.store.trades.get(ticket)
        if plan["action"] == "close":
            self.trading.close_trade(ticket, None)
        elif plan["action"] == "close_partial":
            pos = self.trading.broker.position(ticket)
            if pos is None:
                raise TradeError("Position already closed")
            step = self.trading.engine.info.vol_step
            vol = max(self.trading.engine.info.vol_min, round(int(pos.volume * plan["close_pct"] / 100 / step + 1e-9) * step, 8))
            self.trading.close_trade(ticket, vol)
        elif plan["action"] == "move_sl":
            if "sl_pips" in plan:                                       # stored by an older version
                sl_pips = plan["sl_pips"]
            elif plan["sl"] == "breakeven":
                sl_pips = self.trading.engine.cfg.be_buffer_pips
            else:
                sl_pips = round((plan["sl"] - t.entry) * t.side.sign / PIP, 1)
            self.trading.modify_trade(ticket, sl_pips=sl_pips)
        elif plan["action"] == "modify_tp":
            tp_pips = plan.get("tp_pips") if "tp_pips" in plan else round((plan["tp"] - t.entry) * t.side.sign / PIP, 1)
            steps = self._fit_steps_under(t, tp_pips)
            self.trading.modify_trade(ticket, tp_pips=tp_pips, steps={int(k): v for k, v in steps.items()} or None)

    def _update_waiting_legs(self, plan: dict) -> None:
        """Entries that haven't filled follow the conversation too: a close cancels them,
        a new stop or TP applies to them when they fill."""
        if not plan.get("origin"):
            return
        o = self._get(plan["origin"])
        legs = (o.plan or {}).get("legs") or []
        if not any(l["status"] == "waiting" for l in legs):
            return
        p = ParsedSignal(**o.parsed)
        if plan["action"] in ("close", "close_partial"):
            for l in legs:
                if l["status"] == "waiting":
                    l["status"] = "cancelled"
            o.note = "Waiting entries cancelled: the channel took profit or closed"
        elif plan["action"] == "move_sl" and plan.get("sl") not in (None, "breakeven"):
            p.sl = plan["sl"]
        elif plan["action"] == "modify_tp" and plan.get("tp") is not None:
            p.tps = sorted(set(p.tps[:-1] + [plan["tp"]]), reverse=p.side == "sell")
        o.parsed = p.to_dict()
        if all(l["status"] != "waiting" for l in legs):
            o.plan.pop("expires_at", None)
            if not o.tickets:
                o.status = "expired"
        self._save(o)

    def _armor(self, sig: Signal, ch: Channel) -> None:
        """Breakeven armor: the channel says a target was hit, so protect what's open from it."""
        if not self.cfg.be_on_tp_hit:
            return
        if self.trading.automation_paused:
            sig.note = "Automatic trading is paused: stop not moved"
            return
        origin, trades = self._targets(ch, sig.reply_to)
        if origin is None:
            return
        moved, skipped = [], []
        for t in trades:
            be = t.entry + t.side.sign * self.trading.engine.cfg.be_buffer_pips * PIP
            if t.sl is not None and (t.sl - be) * t.side.sign >= 0:
                continue                                                  # already at or past breakeven
            try:
                self.trading.modify_trade(t.ticket, sl_pips=self.trading.engine.cfg.be_buffer_pips)
                moved.append(t.ticket)
            except TradeError as ex:
                skipped.append(f"#{t.ticket} ({ex})")
        self._update_waiting_legs({"action": "close", "origin": origin.id})   # TP hit: don't add deeper entries
        notes = []
        if moved:
            notes.append(f"SL to breakeven on {_list_tickets(moved)}")
        if skipped:
            notes.append("could not move " + ", ".join(skipped))
        if notes:
            sig.note = "; ".join(notes)
            sig.plan = {"action": "move_sl", "sl": "breakeven", "tickets": moved, "origin": origin.id, "auto": "tp_hit"}

    @staticmethod
    def _fit_steps_under(trade, tp_pips: float) -> dict:
        """A new TP closer than P3: squeeze the steps that haven't fired yet to sit below it."""
        from engine.models import StepStatus
        pending = [i for i, st in enumerate(trade.steps) if st.status is StepStatus.PENDING]
        if not pending or trade.settings.steps[2].pips < tp_pips:
            return {}
        floor = max([trade.settings.steps[i].pips for i in range(3) if i not in pending], default=0.0)
        room = tp_pips - floor
        if room <= len(pending) * 2:
            return {}
        gap = room / (len(pending) + 1)
        out = {}
        for n, i in enumerate(pending, 1):
            pips = round(floor + gap * n, 1)
            move = trade.settings.steps[i].move_sl_pips
            if move is not None and move >= pips:
                move = round(pips / 2, 1)
            out[i] = {"pips": pips, "move_sl_pips": move}
        return out

    # ------------------------------------------------------------------ tick

    def tick(self) -> None:
        """Ladder legs reaching their price, stop waits running out, unfilled entries expiring."""
        now = time.time()
        with self.lock:
            for s in self.store.recent_signals(300):
                if s["status"] == "waiting_sl" and now >= (s.get("plan") or {}).get("wait_until", 0):
                    ch = self.channel(s["channel_id"])
                    sig = _sig(s)
                    if ch is None or self.cfg.no_sl_action == "skip":
                        sig.status, sig.error = "expired", "The channel never gave a stop loss"
                        self._save(sig)
                    else:
                        sig.note = f"No SL after {self.cfg.wait_for_sl_s:g} s: entering with the default stop"
                        self._handle_open(sig, ch, ParsedSignal(**sig.parsed), waited=True)
                elif self.trading.automation_paused:
                    continue                                # waiting entries stand still while paused
                elif s["status"] in ("armed", "placed") and any(l.get("status") == "waiting"
                                                                for l in (s.get("plan") or {}).get("legs", [])):
                    sig = _sig(s)
                    if now >= sig.plan.get("expires_at", float("inf")):
                        for l in sig.plan["legs"]:
                            if l["status"] == "waiting":
                                l["status"] = "expired"
                        sig.plan.pop("expires_at", None)
                        if not sig.tickets:
                            sig.status, sig.error = "expired", "No entry price was reached in time"
                        self._save(sig)
                        continue
                    ch = self.channel(s["channel_id"])
                    if ch is None:
                        continue
                    before = len(sig.tickets)
                    with self.trading.lock:
                        self._fill_legs(sig, ch)
                    self._save(sig)
                    if len(sig.tickets) > before:
                        self.trading.publish(Event("signal", sig.tickets[-1], f"{sig.channel_title}: entry "
                                                   f"{len(sig.tickets)} filled", "Signals"))

    # -------------------------------------------------------------- actions

    def place(self, signal_id: int, lot: float | None = None) -> Signal:
        with self.lock:
            sig = self._get(signal_id)
            if sig.status != "pending":
                raise TradeError(f"Signal is {sig.status}, not pending")
            plan = sig.plan or {}
            if "action" in plan:                       # a follow-up edit waiting for confirmation
                return self._apply(sig)
            if lot is not None:
                plan["settings"]["lot"] = plan["lot"] = lot
                n = len(plan["legs"])
                info = self.trading.engine.info
                from engine.units import floor_volume
                each = max(info.vol_min, floor_volume(lot / n, info.vol_step))
                for i, leg in enumerate(plan["legs"]):
                    leg["lot"] = each if i else round(max(info.vol_min, lot - each * (n - 1)), 8)
            ch = self.channel(sig.channel_id) or Channel(sig.channel_id, sig.channel_title)
            with self.trading.lock:
                return self._arm(sig, ch)

    def dismiss(self, signal_id: int) -> Signal:
        with self.lock:
            sig = self._get(signal_id)
            if sig.status in ("pending", "waiting_sl"):
                sig.status = "dismissed"
                self._save(sig)
            elif sig.status in ("armed", "placed") and sig.plan and any(l["status"] == "waiting" for l in sig.plan.get("legs", [])):
                for l in sig.plan["legs"]:
                    if l["status"] == "waiting":
                        l["status"] = "cancelled"
                if not sig.tickets:
                    sig.status = "dismissed"
                self._save(sig)
            return sig

    def test_parse(self, text: str, channel_id: int | None = None) -> dict:
        """Parse a message and show the plan without storing or trading anything."""
        if self.parser is None:
            raise TradeError("No AI parser: set ANTHROPIC_API_KEY")
        p = normalise(self.parser(text, ""))
        out = {"parsed": p.to_dict(), "plan": None}
        if p.action == "open":
            ch = self.channel(channel_id) if channel_id else None
            ch = ch or Channel(0, "test", weight=MAX_WEIGHT)
            with self.trading.lock:
                quote = self.trading.broker.quote()
                if quote:
                    out["plan"] = self._plan(p, ch, quote)
        return out

    def signals(self, limit: int = 100) -> list[dict]:
        return self.store.recent_signals(limit)

    def timeline(self, signal_id: int) -> dict:
        """One signal's whole conversation: the message, what was read, every entry, the follow-ups and
        channel results about it, and the engine's events on its trades."""
        sig = self._get(signal_id)
        recent = self.store.recent_signals(1000, sig.channel_id)
        origin = sig
        if (sig.parsed or {}).get("action") != "open" and (sig.plan or {}).get("origin"):
            origin = self._get(sig.plan["origin"])
        tickets = set(origin.tickets or ([origin.ticket] if origin.ticket else []))
        related = [s for s in recent if s["id"] != origin.id and s["ts"] >= origin.ts and (
            (s.get("plan") or {}).get("origin") == origin.id or set(_tickets(s)) & tickets
            or set((s.get("plan") or {}).get("tickets") or []) & tickets)]
        with self.trading.lock:
            q = self.trading.broker.quote()
            trades = [self.trading.trade_view(self.store.trades[t], q) for t in sorted(tickets) if t in self.store.trades]
        events = [e for e in self.store.recent_events(1000) if e.get("ticket") in tickets]
        return {"signal": origin.to_dict(), "followups": related, "trades": trades, "events": events}

    # -------------------------------------------------------------- helpers

    def _auto(self, ch: Channel) -> bool:
        """Act without asking: the channel is in auto mode and automatic trading isn't paused."""
        return ch.mode == "auto" and not self.trading.automation_paused

    def _preset(self, ch: Channel):
        from engine import TradeSettings
        presets = self.store.list_presets()
        for p in presets:
            if ch.preset_id is not None and p.id == ch.preset_id:
                return p.settings
        return presets[0].settings if presets else TradeSettings()

    def _get(self, signal_id: int) -> Signal:
        d = self.store.get_signal(signal_id)
        if d is None:
            raise TradeError(f"Unknown signal {signal_id}")
        return _sig(d)

    def _save(self, sig: Signal) -> Signal:
        d = self.store.save_signal(sig.to_dict())
        sig.id = d["id"]
        return sig


def _sig(d: dict) -> Signal:
    return Signal(**{k: v for k, v in d.items() if k in Signal.__dataclass_fields__})


def _tickets(s: dict) -> list[int]:
    return list(s.get("tickets") or ([s["ticket"]] if s.get("ticket") and (s.get("parsed") or {}).get("action") == "open" else []))


def _list_tickets(tickets: list[int]) -> str:
    return ", ".join(f"#{t}" for t in tickets)


def _describe(p: ParsedSignal) -> str:
    """Readable text for a feed signal, shown where a Telegram message would be."""
    if p.action != "open":
        return p.action.replace("_", " ").capitalize()
    parts = [f"{(p.side or '').upper()} XAUUSD" + (f" @ {p.entry:g}" if p.entry is not None else "")]
    if p.sl is not None:
        parts.append(f"SL {p.sl:g}")
    parts += [f"TP{i} {tp:g}" for i, tp in enumerate(p.tps, 1)]
    return " ".join(parts)
