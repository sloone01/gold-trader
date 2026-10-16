"""Receives Telegram messages and feed signals, plans trades by channel trust, and places or queues them."""
from __future__ import annotations

import logging
import threading
import time
import zlib

from engine import Event, Side, TradeError, TradeSettings
from engine.units import PIP

from .mapper import build_plan, settings_from_dict
from .models import MAX_WEIGHT, Channel, ParsedSignal, Signal, SignalSettings, feed_channel_id, is_feed
from .parser import Parser, normalise

log = logging.getLogger("gold-trader.signals")


class SignalService:
    def __init__(self, trading, parser: Parser | None) -> None:
        self.trading = trading          # server.service.TradingService
        self.store = trading.store
        self.parser = parser            # None without ANTHROPIC_API_KEY: feeds still work, Telegram text can't be read
        self.lock = threading.RLock()
        saved = self.store.get_setting("signals") or {}
        self.cfg = SignalSettings(**{k: v for k, v in saved.items() if k in SignalSettings.__dataclass_fields__})
        self.telegram = None            # set by the server when Telegram is configured

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
        self.cfg = cfg
        self.store.put_setting("signals", cfg.to_dict())
        return cfg

    def channel_review(self, channel_id: int) -> dict:
        """Scorecard: how this channel's signals have actually performed with our rules."""
        sigs = [s for s in self.store.recent_signals(1000, channel_id)]
        opens = [s for s in sigs if (s.get("parsed") or {}).get("action") == "open"]
        placed = [s for s in sigs if s.get("ticket") and s["status"] in ("placed", "applied")]
        tickets = {s["ticket"] for s in placed}
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
        # A simple suggestion: enough history and a good hit rate -> trust more; losing -> trust less.
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
        return self._route(sig, ch, parsed)

    def _route(self, sig: Signal, ch: Channel, parsed: ParsedSignal) -> Signal:
        sig.parsed = parsed.to_dict()
        if parsed.action == "ignore":
            sig.status, sig.error = "ignored", parsed.reason
            return self._save(sig)
        if parsed.action == "result":
            sig.status, sig.error = "result", parsed.reason
            return self._save(sig)
        if parsed.action == "watch":
            sig.status, sig.error = "watch", parsed.reason
            self._save(sig)
            self.trading.publish(Event("signal", 0, f"{sig.channel_title}: setup to watch - {parsed.reason}", "Signals"))
            return sig
        if parsed.action == "open":
            return self._handle_open(sig, ch, parsed)
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
                    return Signal(**s)
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
            if s["status"] == "pending":
                return f"Already waiting on signal #{s['id']} for this {side}"
            t = self.store.trades.get(s["ticket"]) if s["status"] == "placed" and s.get("ticket") else None
            if t and t.open:
                return f"Already holding #{t.ticket} for this {side}"
        return None

    def _withdraw_pending(self, channel_id: int) -> None:
        """The feed closed its idea: a trade still waiting for a tap is no longer valid."""
        for s in self.store.recent_signals(200, channel_id):
            if s["status"] == "pending" and (s.get("parsed") or {}).get("action") == "open":
                sig = Signal(**s)
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

    def _handle_open(self, sig: Signal, ch: Channel, p: ParsedSignal) -> Signal:
        with self.trading.lock:
            quote = self.trading.broker.quote()
            if quote is None:
                sig.status, sig.error = "failed", "No quote (market closed?)"
                return self._save(sig)
            preset = self._preset(ch)
            plan = build_plan(p, ch, preset, self.cfg, quote, self.trading.engine.info)
            sig.plan = plan
            if plan["errors"]:
                sig.status, sig.error = "failed", "; ".join(plan["errors"])
                return self._save(sig)
            if ch.mode == "auto":
                return self._place(sig, plan)
        sig.status = "pending"
        self._save(sig)
        self.trading.publish(Event("signal", 0, f"{sig.channel_title}: {p.side} {plan['lot']} lot, SL {p.sl if p.sl is not None else 'default'} - tap to place", "Signals"))
        return sig

    def _place(self, sig: Signal, plan: dict) -> Signal:
        try:
            settings = settings_from_dict(plan["settings"])
            view = self.trading.open_trade(Side(plan["side"]), settings)
            sig.status, sig.ticket = "placed", view["ticket"]
        except TradeError as ex:
            sig.status, sig.error = "failed", str(ex)
        return self._save(sig)

    def _handle_followup(self, sig: Signal, ch: Channel, p: ParsedSignal) -> Signal:
        target = self._last_open_trade(ch.id)
        if target is None:
            sig.status, sig.error = "ignored", f"{p.action}: no open trade from this channel"
            return self._save(sig)
        plan = {"action": p.action, "ticket": target.ticket}
        if p.action == "close_partial":
            plan["close_pct"] = p.close_pct or 50.0
        elif p.action == "move_sl":
            if (p.move_sl_to or "").lower().startswith("break") or not p.move_sl_to:
                plan["sl_pips"] = self.trading.engine.cfg.be_buffer_pips
            else:
                try:
                    plan["sl_pips"] = round((float(p.move_sl_to) - target.entry) * target.side.sign / PIP, 1)
                except ValueError:
                    sig.status, sig.error = "failed", f"Can't read SL price '{p.move_sl_to}'"
                    return self._save(sig)
        elif p.action == "modify_tp":
            if not p.tps:
                sig.status, sig.error = "failed", "No TP price in the message"
                return self._save(sig)
            plan["tp_pips"] = round((p.tps[-1] - target.entry) * target.side.sign / PIP, 1)
            plan["steps"] = self._fit_steps_under(target, plan["tp_pips"])
        sig.plan, sig.ticket = plan, target.ticket
        if ch.mode == "auto":
            return self._apply(sig)
        sig.status = "pending"
        self._save(sig)
        self.trading.publish(Event("signal", target.ticket, f"{sig.channel_title}: {p.action.replace('_', ' ')} on #{target.ticket} - tap to apply", "Signals"))
        return sig

    def _apply(self, sig: Signal) -> Signal:
        plan = sig.plan or {}
        ticket = plan["ticket"]
        try:
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
                self.trading.modify_trade(ticket, sl_pips=plan["sl_pips"])
            elif plan["action"] == "modify_tp":
                steps = {int(k): v for k, v in (plan.get("steps") or {}).items()}
                self.trading.modify_trade(ticket, tp_pips=plan["tp_pips"], steps=steps or None)
            sig.status = "applied"
        except TradeError as ex:
            sig.status, sig.error = "failed", str(ex)
        return self._save(sig)

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

    def _last_open_trade(self, channel_id: int):
        for s in reversed(self.store.recent_signals(200, channel_id)):
            if s["status"] == "placed" and s.get("ticket"):
                t = self.store.trades.get(s["ticket"])
                if t and t.open:
                    return t
        return None

    # -------------------------------------------------------------- actions

    def place(self, signal_id: int, lot: float | None = None) -> Signal:
        sig = self._get(signal_id)
        if sig.status != "pending":
            raise TradeError(f"Signal is {sig.status}, not pending")
        plan = sig.plan or {}
        if "action" in plan:                       # a follow-up edit waiting for confirmation
            return self._apply(sig)
        if lot is not None:
            plan["settings"]["lot"] = plan["lot"] = lot
        return self._place(sig, plan)

    def dismiss(self, signal_id: int) -> Signal:
        sig = self._get(signal_id)
        if sig.status == "pending":
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
                    out["plan"] = build_plan(p, ch, self._preset(ch), self.cfg, quote, self.trading.engine.info)
        return out

    def signals(self, limit: int = 100) -> list[dict]:
        return self.store.recent_signals(limit)

    # -------------------------------------------------------------- helpers

    def _preset(self, ch: Channel) -> TradeSettings:
        presets = self.store.list_presets()
        for p in presets:
            if ch.preset_id is not None and p.id == ch.preset_id:
                return p.settings
        return presets[0].settings if presets else TradeSettings()

    def _get(self, signal_id: int) -> Signal:
        d = self.store.get_signal(signal_id)
        if d is None:
            raise TradeError(f"Unknown signal {signal_id}")
        return Signal(**d)

    def _save(self, sig: Signal) -> Signal:
        d = self.store.save_signal(sig.to_dict())
        sig.id = d["id"]
        return sig


def _describe(p: ParsedSignal) -> str:
    """Readable text for a feed signal, shown where a Telegram message would be."""
    if p.action != "open":
        return p.action.replace("_", " ").capitalize()
    parts = [f"{(p.side or '').upper()} XAUUSD" + (f" @ {p.entry:g}" if p.entry is not None else "")]
    if p.sl is not None:
        parts.append(f"SL {p.sl:g}")
    parts += [f"TP{i} {tp:g}" for i, tp in enumerate(p.tps, 1)]
    return " ".join(parts)
