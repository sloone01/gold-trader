"""The signal agent: one Claude agent that reads every followed channel and decides what each message
means for that channel's trades, the way a human assistant would.

For each new message it sees:
  - shared knowledge (lessons that hold for every channel) and this channel's own notes
  - the channel's live state: its open trades (T1, T2...) and signals still waiting (W1, W2...)
  - the channel's recent conversation and what was done after each message
and answers by calling decision tools (open_trade, close, move_sl, add_entry, ...).

Shadow mode (now): the decisions are recorded next to the parser's result and shown in the app; nothing
trades differently. You teach it by commenting on any signal; the lesson goes into its knowledge and the
message is re-read straight away so you can check it now gets it right.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("gold-trader.agent")

MODEL = "claude-opus-5-5"
MAX_NOTES = 200

SYSTEM = """You are the trading assistant for one person who copies gold (XAUUSD) signals from Telegram
channels into MetaTrader 5. Each turn you get one new message from one channel, together with what you know
about channels in general and about this channel, the channel's open trades and waiting signals, and its
recent messages. Decide what the new message means for THIS channel's trades and answer only by calling the
decision tools. You may call several tools for one message (e.g. close T1 and keep T2, or open a trade and
save a note).

How to read channels:
- Channels have their own styles. Many post the direction first ("buy now", "sell gold") and the stop and
  targets a minute later; a later message with levels completes that signal (use update_levels on its W ref
  or the trade it opened), it is not a new trade.
- Follow-ups refer to trades by meaning: "the old one", "the first entry", "the new one", "both", "all".
  Use the state list (times, entries, sizes) to pick the right refs. A reply to a message usually means the
  trade that message opened.
- "Add more", "add another", "re-enter here" add an entry to the existing position (add_entry), not a new
  idea. "Close the old one and keep the new one" is close on the older ref only.
- Results ("TP1 hit", "+40", "40 pips ✅", "SL hit", "closed at BE") are reports, not instructions: use
  no_action with kind "result". If a target is reported hit and our trade is still open, consider move_sl to
  breakeven only if the knowledge says to.
- Prices may be abbreviated to the last digits ("stop 44" with gold at 2650 means 2644); write full prices.
- Arabic and other languages, emojis and typos are normal. Chat, promotions and analysis are no_action.
- Never invent prices. If a message is too ambiguous to act on safely, use ask_me with a short question.
- Knowledge marked "from you" comes from the person you work for: it overrides your own habits.
- When you notice a lasting habit of this channel (or of channels in general), save it with
  note_for_playbook so you read it better next time. Keep notes short and concrete; don't repeat a note
  that is already in the knowledge.
- Give every tool call a short plain-English reason."""


def _nullable(t: str) -> dict:
    return {"type": [t, "null"]}


def _refs() -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": "Refs from the state, e.g. [\"T1\"] or [\"W1\"]"}


def _tool(name: str, description: str, props: dict) -> dict:
    props = {**props, "reason": {"type": "string"}}
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": props, "required": list(props),
                             "additionalProperties": False}}


TOOLS = [
    _tool("open_trade", "A new trade idea from this channel. entry null = at market; a zone is entry (low) + entry_high.",
          {"side": {"type": "string", "enum": ["buy", "sell"]},
           "order_type": {"type": "string", "enum": ["market", "limit", "stop"]},
           "entry": _nullable("number"), "entry_high": _nullable("number"), "sl": _nullable("number"),
           "tps": {"type": "array", "items": {"type": "number"}}}),
    _tool("wait_for_levels", "The channel gave a direction but no stop yet; remember it and wait for the levels.",
          {"side": {"type": "string", "enum": ["buy", "sell"]}}),
    _tool("update_levels", "The message gives the stop and/or targets for a waiting signal or an open trade.",
          {"targets": _refs(), "sl": _nullable("number"), "tps": {"type": "array", "items": {"type": "number"}}}),
    _tool("add_entry", "Add another entry to an existing position (\"add more\"). entry null = at market.",
          {"to": {"type": "string"}, "entry": _nullable("number")}),
    _tool("close", "Close trades fully (percent 100) or partly.",
          {"targets": _refs(), "percent": {"type": "number"}}),
    _tool("move_sl", "Move the stop of trades: to breakeven, or to a price.",
          {"targets": _refs(), "breakeven": {"type": "boolean"}, "price": _nullable("number")}),
    _tool("set_tp", "Set a new final take profit on trades.", {"targets": _refs(), "tp": {"type": "number"}}),
    _tool("cancel_pending", "Cancel signals or entries that haven't filled yet.", {"targets": _refs()}),
    _tool("ask_me", "Too ambiguous to act on safely: ask the person a short question.", {"question": {"type": "string"}}),
    _tool("note_for_playbook", "Save a lasting lesson: scope channel = this channel's habit, shared = all channels.",
          {"scope": {"type": "string", "enum": ["channel", "shared"]}, "text": {"type": "string"}}),
    _tool("no_action", "Nothing to do: chat, a result report, analysis, or a message already handled.",
          {"kind": {"type": "string", "enum": ["chat", "result", "analysis", "duplicate", "other"]}}),
]
DECISIONS = {t["name"] for t in TOOLS} - {"note_for_playbook"}


class Knowledge:
    """Shared and per-channel notes, kept in the app's settings store."""

    KEY = "agent_knowledge"

    def __init__(self, store) -> None:
        self.store = store
        self.lock = threading.Lock()

    def _data(self) -> dict:
        d = self.store.get_setting(self.KEY) or {}
        return {"shared": d.get("shared", []), "channels": d.get("channels", {}), "next_id": d.get("next_id", 1)}

    def notes(self, channel_id: int | None = None) -> dict:
        d = self._data()
        return {"shared": d["shared"], "channel": d["channels"].get(str(channel_id), []) if channel_id is not None else []}

    def all(self) -> dict:
        d = self._data()
        return {"shared": d["shared"], "channels": d["channels"]}

    def add(self, scope: str, channel_id: int | None, text: str, source: str, signal_id: int | None = None,
            example: str | None = None) -> dict:
        text = " ".join((text or "").split())[:600]
        if not text:
            raise ValueError("Empty note")
        with self.lock:
            d = self._data()
            note = {"id": d["next_id"], "text": text, "source": source, "ts": time.time(),
                    "signal_id": signal_id, "example": (example or "")[:300] or None}
            bucket = d["shared"] if scope == "shared" or channel_id is None else d["channels"].setdefault(str(channel_id), [])
            if any(n["text"].lower() == text.lower() for n in bucket):
                return next(n for n in bucket if n["text"].lower() == text.lower())
            bucket.append(note)
            del bucket[:-MAX_NOTES]
            d["next_id"] += 1
            self.store.put_setting(self.KEY, d)
            return note

    def update(self, note_id: int, text: str | None = None, delete: bool = False) -> bool:
        with self.lock:
            d = self._data()
            for bucket in [d["shared"], *d["channels"].values()]:
                for i, n in enumerate(bucket):
                    if n["id"] == note_id:
                        if delete:
                            bucket.pop(i)
                        else:
                            n["text"] = " ".join((text or "").split())[:600] or n["text"]
                        self.store.put_setting(self.KEY, d)
                        return True
            return False


class SignalAgent:
    def __init__(self, knowledge: Knowledge, client=None, model: str = MODEL) -> None:
        self.knowledge = knowledge
        self.model = model
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client

    # ---------------------------------------------------------------- prompt

    def _knowledge_text(self, channel_id: int, channel_title: str) -> str:
        k = self.knowledge.notes(channel_id)

        def fmt(notes):
            return "\n".join(f"- {n['text']}" + (" (from you)" if n["source"] == "you" else "") +
                             (f"\n  e.g. message: \"{n['example']}\"" if n.get("example") else "") for n in notes) or "- (none yet)"
        return (f"What you know about channels in general:\n{fmt(k['shared'])}\n\n"
                f"What you know about the channel \"{channel_title}\":\n{fmt(k['channel'])}")

    @staticmethod
    def _state_text(state: dict) -> str:
        lines = [f"Current gold price: {state['price']}" if state.get("price") else "Current gold price: unknown"]
        if state["trades"]:
            lines.append("Open trades from this channel:")
            lines += [f"- {t['ref']}: {t['side']} {t['volume']} lot (of {t['opened_volume']}) @ {t['entry']}, opened "
                      f"{t['opened']}, SL {t['sl']}, TP {t['tp']}, profit {t['profit_pips']} pips, steps hit: {t['steps_hit']}"
                      for t in state["trades"]]
        else:
            lines.append("Open trades from this channel: none")
        if state["waiting"]:
            lines.append("Signals waiting (no fill yet):")
            lines += [f"- {w['ref']}: {w['what']}" for w in state["waiting"]]
        return "\n".join(lines)

    # ----------------------------------------------------------------- decide

    def decide(self, channel_id: int, channel_title: str, text: str, state: dict, recent: list[dict],
               reply_text: str | None = None, image: bytes | None = None, image_type: str = "image/jpeg") -> dict:
        """Returns {actions: [{tool, input}], notes_saved: [...], summary, model, usage, error}."""
        convo = "\n".join(f"[{r['time']}] {r['text']}" + (f"  -> {r['done']}" if r.get("done") else "") for r in recent) or "(no earlier messages)"
        user = (f"{self._state_text(state)}\n\nRecent messages in this channel (oldest first):\n{convo}\n\n"
                + (f"The new message replies to: \"{reply_text}\"\n\n" if reply_text else "")
                + f"New message:\n{text or '(image only)'}")
        content: list[dict] = []
        if image:
            import base64
            content.append({"type": "image", "source": {"type": "base64", "media_type": image_type,
                                                        "data": base64.standard_b64encode(image).decode()}})
        content.append({"type": "text", "text": user})
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "medium"},
                # Stable rules first, then the knowledge (changes only when a note is added), both cached.
                system=[{"type": "text", "text": SYSTEM},
                        {"type": "text", "text": self._knowledge_text(channel_id, channel_title),
                         "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS,
                messages=[{"role": "user", "content": content}],
            )
        except Exception as ex:          # the agent is advisory: never let it break signal intake
            log.exception("agent call failed")
            return {"actions": [], "notes_saved": [], "summary": "", "model": self.model, "error": str(ex)}
        if resp.stop_reason == "refusal":
            return {"actions": [], "notes_saved": [], "summary": "", "model": resp.model, "error": "The model declined to read this message"}
        actions, notes, texts = [], [], []
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                texts.append(b.text.strip())
            elif b.type == "tool_use" and b.name in DECISIONS:
                actions.append({"tool": b.name, "input": dict(b.input)})
            elif b.type == "tool_use" and b.name == "note_for_playbook":
                try:
                    n = self.knowledge.add(b.input.get("scope", "channel"), channel_id, b.input.get("text", ""), "agent",
                                           example=text)
                    notes.append({"scope": b.input.get("scope"), "text": n["text"]})
                except ValueError:
                    pass
        u = resp.usage
        return {"actions": actions, "notes_saved": notes, "summary": " ".join(texts)[:500], "model": resp.model,
                "error": None if actions else "The agent made no decision",
                "usage": {"input": u.input_tokens, "output": u.output_tokens,
                          "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0}}


def describe(action: dict) -> str:
    """One readable line for an agent decision."""
    a, i = action["tool"], action["input"]
    t = ", ".join(i.get("targets") or []) or i.get("to") or ""
    if a == "open_trade":
        lv = (f"{i['order_type']} @ {i['entry']}" + (f"-{i['entry_high']}" if i.get("entry_high") else "")) if i.get("entry") else "at market"
        return f"Open {i['side'].upper()} {lv}, SL {i['sl'] if i.get('sl') is not None else '?'}, TP {', '.join(map(str, i.get('tps') or [])) or '?'}"
    if a == "wait_for_levels":
        return f"Wait for the stop and targets of a {i['side'].upper()}"
    if a == "update_levels":
        return f"Set levels on {t}: SL {i.get('sl')}, TP {', '.join(map(str, i.get('tps') or [])) or '-'}"
    if a == "add_entry":
        return f"Add an entry to {t} " + (f"@ {i['entry']}" if i.get("entry") else "at market")
    if a == "close":
        return f"Close {i['percent']:g}% of {t}" if i.get("percent", 100) < 100 else f"Close {t}"
    if a == "move_sl":
        return f"Move SL of {t} to " + ("breakeven" if i.get("breakeven") else str(i.get("price")))
    if a == "set_tp":
        return f"Set TP of {t} to {i['tp']}"
    if a == "cancel_pending":
        return f"Cancel {t}"
    if a == "ask_me":
        return f"Asks you: {i['question']}"
    return f"No action ({i.get('kind', 'other')})"
