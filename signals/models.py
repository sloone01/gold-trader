"""Telegram signal channels, parsed signals and the trades planned from them."""
from __future__ import annotations

import zlib
from dataclasses import asdict, dataclass, field

MAX_WEIGHT = 5

# Feeds are channels pushed to us over the API by another app (e.g. Market Watch) instead of
# read from Telegram. Their ids sit far above any Telegram id so the two never collide.
FEED_ID_BASE = 9_000_000_000_000


def feed_channel_id(feed: str) -> int:
    return FEED_ID_BASE + zlib.crc32(feed.encode())


def is_feed(channel_id: int) -> bool:
    return int(channel_id) >= FEED_ID_BASE


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
    # Sizing: "weight" = lot_per_weight x weight; "risk" = weight x risk_pct_per_weight % of the balance,
    # lot worked out from the stop distance (so a wide stop trades smaller).
    sizing: str = "weight"
    risk_pct_per_weight: float = 0.25   # weight 1 risks 0.25% of the balance, weight 5 risks 1.25%
    # A signal without a stop waits this long for the channel's "SL ..." message before entering.
    wait_for_sl_s: float = 90.0         # 0 = enter at once with the default stop
    no_sl_action: str = "default"       # when the wait runs out: default (use default_sl_pips) | skip
    # Entry ladder: split the lot over up to 3 entries (zone edges, or deeper toward the stop).
    ladder: bool = False
    ladder_legs: int = 3
    ladder_depth_pct: float = 50.0      # deepest leg sits this % of the way from the entry to the stop
    ladder_expiry_min: float = 240.0    # unfilled entries are cancelled after this
    # Breakeven armor: when the channel posts "TP1 hit", move the stop of its trades to breakeven.
    be_on_tp_hit: bool = True
    # The signal agent: off | shadow (reads every message and shows what it would do; nothing trades differently)
    agent_mode: str = "off"

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
    order_type: str = "market"      # market | limit | stop (pending orders wait for their price)
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
    # ignored | pending | placed | applied | failed | dismissed | result | watch
    # waiting_sl (no stop yet) | armed (entries waiting for their price) | expired
    status: str = "new"
    parsed: dict | None = None
    plan: dict | None = None        # the trade we would (or did) place / the edit we would apply
    ticket: int | None = None       # the first (or only) trade
    error: str | None = None
    reply_to: int | None = None
    tickets: list[int] = field(default_factory=list)   # every trade from this signal (ladder legs)
    note: str | None = None         # what else happened, e.g. "SL to breakeven on #12"

    def to_dict(self) -> dict:
        return asdict(self)
