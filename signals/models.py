"""Telegram signal channels, parsed signals and the trades planned from them."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

MAX_WEIGHT = 5


@dataclass
class Channel:
    """A Telegram channel/group the bot listens to.

    weight: 1 (barely trusted) .. 5 (fully trusted). It scales the lot size.
    mode: off | confirm (signals wait for a tap) | auto (placed immediately)
    """
    id: int
    title: str
    weight: int = 3
    mode: str = "confirm"
    preset_id: int | None = None    # which preset supplies the step rules; None = first preset
    topics: list[dict] = field(default_factory=list)   # [{id, title}] for groups with topics; [] = whole group

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Channel":
        d = dict(d)
        if d.get("topic_id") is not None and not d.get("topics"):      # saved by the single-topic version
            d["topics"] = [{"id": d["topic_id"], "title": d.get("topic_title") or str(d["topic_id"])}]
        d = {k: v for k, v in d.items() if k in Channel.__dataclass_fields__}
        return Channel(**d)

    def topic_ids(self) -> set[int]:
        return {int(t["id"]) for t in self.topics}

    def topic_title(self, topic_id: int | None) -> str | None:
        return next((t.get("title") for t in self.topics if int(t["id"]) == topic_id), None)


@dataclass
class SignalSettings:
    lot_per_weight: float = 0.01    # lot = weight x this: weight 1 -> 0.01, weight 5 -> 0.05
    use_signal_tps: bool = True     # take P1-P3 distances from the signal's TPs when it has three
    final_tp: bool = False          # also set the last TP of the signal as the hard TP
    max_entry_drift_pips: float = 30.0  # warn when market is further than this from the signal's entry
    default_sl_pips: float = 50.0   # stop used when the signal gives none (updated when a "stop X" message follows)
    model: str = "claude-opus-5"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParsedSignal:
    """What the AI extracted from one message."""
    action: str = "ignore"          # open | close | close_partial | move_sl | modify_tp | ignore
    symbol: str | None = None       # normalised, e.g. XAUUSD
    side: str | None = None         # buy | sell
    entry: float | None = None      # price, or midpoint of a range; None = market
    entry_high: float | None = None
    sl: float | None = None
    tps: list[float] = field(default_factory=list)
    close_pct: float | None = None  # for close_partial
    move_sl_to: str | None = None   # "breakeven" or a price as text
    result: str | None = None       # for action "result": tp | sl | be | pips
    result_pips: float | None = None  # claimed outcome in pips (+ profit, - loss) when stated
    result_tp: int | None = None    # which TP was reported hit (1, 2, 3...)
    confidence: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Signal:
    id: int | None
    channel_id: int
    channel_title: str
    msg_id: int
    text: str
    ts: float
    status: str = "new"             # ignored | pending | placed | applied | failed | dismissed
    parsed: dict | None = None
    plan: dict | None = None        # the trade we would (or did) place / the edit we would apply
    ticket: int | None = None
    error: str | None = None
    reply_to: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)
