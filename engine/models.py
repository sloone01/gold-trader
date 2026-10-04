"""Data types shared by the engine, the store and (later) the API."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class StopMode(str, Enum):
    TIGHT = "tight"    # 20 pips
    MEDIUM = "medium"  # 50 pips
    FAR = "far"        # set by the user, before or after entry


STOP_MODE_PIPS = {StopMode.TIGHT: 20.0, StopMode.MEDIUM: 50.0}


class StepStatus(str, Enum):
    PENDING = "pending"
    HIT = "hit"
    FAILED = "failed"


@dataclass
class StepConfig:
    pips: float                        # distance from entry that triggers the step
    close_pct: float = 0.0             # share of the ORIGINAL volume to close (0 = none)
    move_sl_pips: float | None = None  # None = don't move, 0 = breakeven, N = +N pips


@dataclass
class TradeSettings:
    """Everything a preset stores, plus any per-trade overrides."""
    lot: float = 0.04
    stop_mode: StopMode = StopMode.MEDIUM
    sl_pips: float | None = None   # only used in FAR mode; tight/medium come from STOP_MODE_PIPS
    tp_pips: float | None = None   # None = no final TP
    steps: list[StepConfig] = field(default_factory=lambda: [
        StepConfig(20, 50, 0),
        StepConfig(50, 25, 20),
        StepConfig(100, 0, 50),
    ])
    trailing: bool = True
    trail_pips: float = 50.0

    def effective_sl_pips(self) -> float | None:
        if self.stop_mode is StopMode.FAR:
            return self.sl_pips
        return STOP_MODE_PIPS[self.stop_mode]

    @staticmethod
    def from_dict(d: dict) -> "TradeSettings":
        d = dict(d)
        d["stop_mode"] = StopMode(d.get("stop_mode", "medium"))
        if "steps" in d:
            d["steps"] = [StepConfig(**s) for s in d["steps"]]
        return TradeSettings(**d)


@dataclass
class Preset:
    name: str
    settings: TradeSettings
    id: int | None = None


@dataclass
class StepState:
    status: StepStatus = StepStatus.PENDING
    planned_volume: float = 0.0
    close_done: bool = False
    sl_done: bool = False
    attempts: int = 0
    next_attempt_at: float = 0.0
    closed_volume: float = 0.0
    close_price: float | None = None
    error: str | None = None


@dataclass
class Trade:
    ticket: int
    side: Side
    settings: TradeSettings
    entry: float
    volume: float          # original volume
    sl: float | None
    tp: float | None
    steps: list[StepState]
    trail_k: int = 0       # number of trailing moves done after P3
    open: bool = True
    opened_at: float = 0.0
    closed_at: float | None = None
    result_pips: float | None = None
    result_usd: float | None = None
    close_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Trade":
        d = dict(d)
        d["side"] = Side(d["side"])
        d["settings"] = TradeSettings.from_dict(d["settings"])
        d["steps"] = [StepState(**{**s, "status": StepStatus(s["status"])}) for s in d["steps"]]
        return Trade(**d)


@dataclass
class Event:
    kind: str       # opened | step_hit | sl_moved | closed | error
    ticket: int
    message: str
    action: str | None = None  # View | Re-enter | Retry
