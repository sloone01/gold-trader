"""Market Watch settings: timeframes, the asset universe, and options read from .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

TIMEFRAMES = {
    "5m":  {"id": "5m",  "label": "5M",  "label_ar": "٥ د",     "td": "5min",  "binance": "5m",  "minutes": 5,    "points": 200},
    "15m": {"id": "15m", "label": "15M", "label_ar": "١٥ د",    "td": "15min", "binance": "15m", "minutes": 15,   "points": 200},
    "1h":  {"id": "1h",  "label": "1H",  "label_ar": "ساعة",    "td": "1h",    "binance": "1h",  "minutes": 60,   "points": 200},
    "4h":  {"id": "4h",  "label": "4H",  "label_ar": "4 ساعات", "td": "4h",    "binance": "4h",  "minutes": 240,  "points": 200},
    "1d":  {"id": "1d",  "label": "1D",  "label_ar": "يوم",     "td": "1day",  "binance": "1d",  "minutes": 1440, "points": 200},
}
DEFAULT_TIMEFRAME = "4h"

# provider: where prices come from. "broker" (gold only) = the MT5 terminal this app trades on.
ASSETS = [
    {"id": "gold",     "name": "Gold",      "name_ar": "الذهب",        "kind": "metal",  "provider": "twelvedata", "td": "XAU/USD", "yahoo": "GC=F",     "unit": "USD/oz"},
    {"id": "silver",   "name": "Silver",    "name_ar": "الفضة",        "kind": "metal",  "provider": "twelvedata", "td": "XAG/USD", "yahoo": "SI=F",     "unit": "USD/oz"},
    {"id": "oil",      "name": "Crude Oil", "name_ar": "النفط الخام",  "kind": "energy", "provider": "twelvedata", "td": "WTI/USD", "yahoo": "CL=F",     "unit": "USD/bbl"},
    {"id": "eurusd",   "name": "EUR / USD", "name_ar": "يورو / دولار", "kind": "forex",  "provider": "twelvedata", "td": "EUR/USD", "yahoo": "EURUSD=X", "unit": ""},
    {"id": "gbpusd",   "name": "GBP / USD", "name_ar": "جنيه / دولار", "kind": "forex",  "provider": "twelvedata", "td": "GBP/USD", "yahoo": "GBPUSD=X", "unit": ""},
    {"id": "usdjpy",   "name": "USD / JPY", "name_ar": "دولار / ين",   "kind": "forex",  "provider": "twelvedata", "td": "USD/JPY", "yahoo": "USDJPY=X", "unit": ""},
    {"id": "bitcoin",  "name": "Bitcoin",   "name_ar": "بيتكوين",      "kind": "crypto", "provider": "binance",    "binance": "BTCUSDT", "unit": "USD"},
    {"id": "ethereum", "name": "Ethereum",  "name_ar": "إيثيريوم",     "kind": "crypto", "provider": "binance",    "binance": "ETHUSDT", "unit": "USD"},
]


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "")
    return default if v == "" else v.strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _list(name: str, default: str = "") -> list[str]:
    return [s.strip().lower() for s in os.environ.get(name, default).split(",") if s.strip()]


@dataclass
class MarketConfig:
    enabled: bool = True
    refresh_seconds: float = 120
    timeframes: list[str] = field(default_factory=lambda: list(TIMEFRAMES))
    twelvedata_key: str = ""
    marketaux_key: str = ""
    alert_bot_token: str = ""       # Telegram bot for signal-flip alerts (not the listener account)
    alert_chat_id: str = ""
    # News blackout: no new entries this long around high-impact releases
    news_filter: bool = True
    news_before_min: float = 60
    news_after_min: float = 180
    news_impacts: list[str] = field(default_factory=lambda: ["high"])
    # The gold feed: the gold idea goes to the signal service as the "Market Watch" channel
    feed: bool = False
    feed_timeframe: str = DEFAULT_TIMEFRAME
    feed_min_confidence: float = 40
    feed_min_score: float = 20
    feed_tp_rs: list[float] = field(default_factory=lambda: [1.0, 2.0, 3.0])
    feed_close_on_inactive: bool = False

    @staticmethod
    def from_env() -> "MarketConfig":
        tfs = [t for t in _list("MW_TIMEFRAMES", ",".join(TIMEFRAMES)) if t in TIMEFRAMES]
        feed_tf = os.environ.get("MW_FEED_TIMEFRAME", DEFAULT_TIMEFRAME)
        return MarketConfig(
            enabled=_bool("MW_ENABLED", False),
            refresh_seconds=max(30.0, _num("MW_REFRESH_SECONDS", 120)),
            timeframes=tfs or [DEFAULT_TIMEFRAME],
            twelvedata_key=os.environ.get("TWELVEDATA_API_KEY", ""),
            marketaux_key=os.environ.get("MARKETAUX_API_KEY", ""),
            alert_bot_token=os.environ.get("MW_ALERT_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", ""),   # old names work
            alert_chat_id=os.environ.get("MW_ALERT_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", ""),
            news_filter=_bool("NEWS_FILTER_ENABLED", True),
            news_before_min=_num("NEWS_BLOCK_BEFORE_MIN", 60),
            news_after_min=_num("NEWS_BLOCK_AFTER_MIN", 180),
            news_impacts=_list("NEWS_BLOCK_IMPACTS", "high"),
            feed=_bool("MW_FEED_ENABLED", False),
            feed_timeframe=feed_tf if feed_tf in TIMEFRAMES else DEFAULT_TIMEFRAME,
            feed_min_confidence=_num("MW_FEED_MIN_CONFIDENCE", 40),
            feed_min_score=_num("MW_FEED_MIN_SCORE", 20),
            feed_tp_rs=[float(x) for x in _list("MW_FEED_TP_RS", "1,2,3") if _is_pos(x)],
            feed_close_on_inactive=_bool("MW_FEED_CLOSE_ON_INACTIVE", False),
        )


def _is_pos(x: str) -> bool:
    try:
        return float(x) > 0
    except ValueError:
        return False
