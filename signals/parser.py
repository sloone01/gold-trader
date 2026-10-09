"""Turn a Telegram message into a ParsedSignal with Claude.

The parser is a plain callable `(text, context) -> ParsedSignal` so tests can swap in a fake.
"""
from __future__ import annotations

import logging
from typing import Protocol

from pydantic import BaseModel, Field

from .models import ParsedSignal

log = logging.getLogger("gold-trader.signals")

SYSTEM = """You read messages from gold (XAU/USD) trading-signal Telegram channels and extract
the trade instruction as structured data. Be strict and literal: never invent prices.

Rules:
- symbol: normalise GOLD, XAUUSD, XAU/USD, XAU-USD, GOLD#, GOLD.m etc. to "XAUUSD". Any other
  instrument (BTC, EURUSD, indices...) -> action "ignore" with the reason.
- action "open": a new buy/sell with at least a direction. Entry may be a price, a range
  ("buy 2650-2655": entry = low, entry_high = high), or missing (market). Collect the stop loss
  as sl and every take-profit as tps in the order given ("TP1 2660, TP2 2670" -> [2660, 2670]).
  If TPs are given as "+20 pips" style distances, leave tps empty and explain in reason.
  Pending orders ("buy limit", "sell stop") are still "open"; note the order type in reason.
- action "close": close the whole running trade ("close now", "exit", "take full profit").
- action "close_partial": close part of it; close_pct is the percentage if stated ("close half" -> 50),
  else null.
- action "move_sl": move the stop; move_sl_to is "breakeven" for BE/entry/"risk free", otherwise
  the price as text.
- action "modify_tp": a new TP for the running trade; put the new TP(s) in tps.
- action "watch": a setup that is NOT an instruction to trade now but tells readers what to wait
  for: "buy if it breaks and retests 4150", "watch the zone 4120-4125 for a reaction", a pending
  order idea with a condition. Fill side/entry/sl/tps with whatever levels are given and put the
  condition in reason. Conditional language ("if", "when it breaks", "wait for") means "watch", not "open".
- A message may be a chart image with or without a caption. Read levels drawn or written on the
  chart (entry zones, SL, TP lines, arrows for direction). If the image shows a concrete trade with
  a direction and levels and the caption says to enter now, it is "open"; if it is an idea or a
  zone to watch, it is "watch"; if it is just a result screenshot or analysis, "ignore".
- action "result": the channel reports an outcome of its own call: "TP1 hit", "TP2 done +100 pips",
  "+40", "150+", "stop loss hit", "closed at breakeven", "booked 170". Set result to "tp" (a target hit,
  result_tp = its number if given), "sl" (stop hit), "be" (closed at entry) or "pips" (a profit/loss
  figure alone), and result_pips to the number when one is stated (profit positive, loss negative).
  A bare "+40" or "40+" is a result of 40 pips. Do not treat these as trade instructions.
- Anything else (chat, promotions, analysis without an instruction) -> "ignore".
- Numbers: Arabic-Indic digits (٠١٢٣٤٥٦٧٨٩) are ordinary numbers. Traders often abbreviate a gold
  price to its last two or three digits: with gold near 4150, "stop 44" means 4144, "targets 55 and
  65" mean 4155 and 4165, "only buy above 45" means 4145. Use the current price given to expand
  such levels, and keep the expanded full price in the fields. "+20", "40+" alone are pip results, not levels.
- Messages in a channel are often split: "entered sell now", then "4120" (the entry), then
  "stop 4125", then "targets ...". Parse each on its own: a lone price after an entry message is
  context, not an instruction; "the stop X" is move_sl to X; "your targets ..." is modify_tp.
- confidence: 0-1, how sure you are the extraction is right and the message is a real instruction.
- reason: one short plain sentence in English, at most 25 words, no decorative or repeated characters."""


class SignalOut(BaseModel):
    action: str = Field(description="open | close | close_partial | move_sl | modify_tp | watch | result | ignore")
    symbol: str | None = None
    side: str | None = Field(default=None, description="buy | sell")
    entry: float | None = None
    entry_high: float | None = None
    sl: float | None = None
    tps: list[float] = Field(default_factory=list)
    close_pct: float | None = None
    move_sl_to: str | None = None
    result: str | None = Field(default=None, description="for action result: tp | sl | be | pips")
    result_pips: float | None = None
    result_tp: int | None = None
    confidence: float = 0.0
    reason: str = ""


class Parser(Protocol):
    def __call__(self, text: str, context: str = "", image: bytes | None = None,
                 image_type: str = "image/jpeg") -> ParsedSignal: ...


class ClaudeParser:
    def __init__(self, model: str = "claude-opus-5", client=None) -> None:
        import os
        import anthropic
        # An organisation-level key must name a workspace; a key created inside a workspace doesn't need this.
        ws = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
        headers = {"anthropic-workspace-id": ws} if ws else None
        self.client = client or anthropic.Anthropic(default_headers=headers)
        self.model = model

    def __call__(self, text: str, context: str = "", image: bytes | None = None,
                 image_type: str = "image/jpeg") -> ParsedSignal:
        import base64
        user = text if not context else f"{context}\n\nNew message:\n{text}"
        content: list[dict] = []
        if image:
            content.append({"type": "image", "source": {"type": "base64", "media_type": image_type,
                                                        "data": base64.standard_b64encode(image).decode()}})
        content.append({"type": "text", "text": user or "(image only, no caption)"})
        import pydantic
        try:
            response = self._parse(content)
        except pydantic.ValidationError as ex:
            log.warning("unparseable model output: %s", str(ex)[:200])
            return ParsedSignal(action="ignore", reason="The model's answer could not be read", confidence=0.0)
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return ParsedSignal(action="ignore", reason="The model did not return a parse", confidence=0.0)
        out: SignalOut = response.parsed_output
        out.reason = (out.reason or "")[:200]
        return ParsedSignal(**out.model_dump())

    def _parse(self, content: list[dict]):
        return self.client.messages.parse(
            model=self.model,
            max_tokens=4000,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": content}],
            output_format=SignalOut,
        )


def normalise(p: ParsedSignal) -> ParsedSignal:
    """Tidy values the model may leave inconsistent."""
    if p.symbol:
        s = p.symbol.upper().replace("/", "").replace("-", "").replace("#", "").replace(".M", "").replace("GOLD", "XAUUSD")
        p.symbol = "XAUUSD" if "XAU" in s else s
    if p.side:
        p.side = p.side.lower()
        if p.side not in ("buy", "sell"):
            p.side = None
    p.action = (p.action or "ignore").lower()
    if p.action not in ("open", "close", "close_partial", "move_sl", "modify_tp", "watch", "result"):
        p.action = "ignore"
    if p.action == "result":
        p.result = (p.result or "pips").lower()
        if p.result not in ("tp", "sl", "be", "pips"):
            p.result = "pips"
        if p.result == "sl" and p.result_pips is not None and p.result_pips > 0:
            p.result_pips = -p.result_pips
    if p.action == "open" and (not p.side or p.symbol != "XAUUSD"):
        p.action, p.reason = "ignore", p.reason or "Not a gold trade with a direction"
    if p.action == "watch" and p.symbol not in (None, "XAUUSD"):
        p.action, p.reason = "ignore", p.reason or "Setup is not for gold"
    if p.entry is not None and p.entry_high is not None and p.entry_high < p.entry:
        p.entry, p.entry_high = p.entry_high, p.entry
    p.tps = [float(t) for t in p.tps if t]
    return p
