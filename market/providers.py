"""Price, news and calendar feeds.

Gold can use the broker's own candles (the MT5 terminal this app trades on), so the analysis
sees the prices orders fill at. Otherwise: metals/FX/oil from Twelve Data with a Yahoo
fallback, crypto from Binance, and demo data (clearly labelled) when everything fails.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger("gold-trader.market")
UA = {"user-agent": "Mozilla/5.0 GoldTrader/1.0", "accept": "application/json"}


class FeedError(RuntimeError):
    pass


# ---------------------------------------------------------------- cache

class Cache:
    """TTL cache that keeps serving the last good value (marked stale) when a refresh fails."""

    def __init__(self) -> None:
        self._d: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float, loader):
        with self._lock:
            hit = self._d.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        try:
            value = loader()
        except Exception:
            if hit:
                with self._lock:
                    self._d[key] = (time.time() + min(ttl, 120), hit[1])
                return {**hit[1], "stale": True} if isinstance(hit[1], dict) else hit[1]
            raise
        with self._lock:
            self._d[key] = (time.time() + ttl, value)
        return value


CACHE = Cache()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(url: str, params: dict | None = None, timeout: float = 12) -> httpx.Response:
    return httpx.get(url, params=params, headers=UA, timeout=timeout, follow_redirects=True)


# ---------------------------------------------------------------- prices

TD_FALLBACKS = {"XAG/USD": ["XAG/USD", "XAGUSD"], "XAU/USD": ["XAU/USD", "XAUUSD"],
                "WTI/USD": ["WTI/USD", "CLUSD", "WTIUSD"]}


def twelvedata(asset: dict, tf: dict, key: str) -> dict:
    if not key:
        raise FeedError("NO_KEY")

    def load():
        last = None
        intervals = ["1day"] if tf["td"] == "1day" else [tf["td"], "1day"]
        for sym in TD_FALLBACKS.get(asset["td"], [asset["td"]]):
            for iv in intervals:
                try:
                    r = _get("https://api.twelvedata.com/time_series",
                             {"symbol": sym, "interval": iv, "outputsize": tf["points"], "apikey": key})
                    if r.status_code == 429:
                        raise FeedError(f"Twelve Data 429 for {sym}")
                    j = r.json()
                    if r.status_code != 200 or j.get("status") == "error":
                        raise FeedError(f"Twelve Data: {j.get('message') or r.status_code}")
                    vals = j.get("values") or []
                    if len(vals) < 2:
                        raise FeedError(f"Twelve Data returned no data for {sym}")
                    candles = [{"time": v["datetime"], "open": float(v["open"]), "high": float(v["high"]),
                                "low": float(v["low"]), "close": float(v["close"])} for v in reversed(vals)]
                    return {"candles": candles, "price": candles[-1]["close"], "source": "twelvedata"}
                except FeedError as ex:
                    last = ex
                    if "429" in str(ex):          # don't burn fallbacks on a rate limit
                        raise
        raise last or FeedError("Twelve Data failed")
    return CACHE.get(f"td:{asset['td']}:{tf['td']}", 300, load)


def _yahoo_params(tf_id: str) -> tuple[str, str, int]:
    return {"1d": ("1d", "2y", 1), "4h": ("1h", "60d", 4), "15m": ("15m", "60d", 1),
            "5m": ("5m", "60d", 1)}.get(tf_id, ("1h", "60d", 1))


def aggregate(candles: list[dict], n: int) -> list[dict]:
    if n <= 1:
        return candles
    out = []
    for i in range(0, len(candles) - n + 1, n):
        s = candles[i:i + n]
        out.append({"time": s[0]["time"], "open": s[0]["open"], "high": max(c["high"] for c in s),
                    "low": min(c["low"] for c in s), "close": s[-1]["close"]})
    return out


def yahoo(asset: dict, tf: dict) -> dict:
    sym = asset.get("yahoo")
    if not sym:
        raise FeedError("NO_YAHOO_SYMBOL")
    interval, rng, agg = _yahoo_params(tf["id"])

    def load():
        r = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", {"interval": interval, "range": rng})
        if r.status_code != 200:
            raise FeedError(f"Yahoo {r.status_code} for {sym}")
        res = ((r.json().get("chart") or {}).get("result") or [None])[0]
        if not res or not res.get("timestamp"):
            raise FeedError(f"Yahoo returned no data for {sym}")
        q = (res.get("indicators") or {}).get("quote", [{}])[0]
        candles = []
        for i, ts in enumerate(res["timestamp"]):
            vals = [(q.get(k) or [None] * (i + 1))[i] for k in ("open", "high", "low", "close")]
            if any(v is None for v in vals):
                continue
            o, h, lo, c = map(float, vals)
            candles.append({"time": _iso(ts), "open": o, "high": h, "low": lo, "close": c})
        series = aggregate(candles, agg)
        if len(series) < 30:
            raise FeedError(f"Yahoo series too short for {sym}")
        series = series[-tf["points"]:]
        return {"candles": series, "price": series[-1]["close"], "source": "yahoo"}
    return CACHE.get(f"yh:{sym}:{tf['id']}", 180, load)


def binance(asset: dict, tf: dict) -> dict:
    sym = asset.get("binance")
    if not sym:
        raise FeedError("NO_BINANCE_SYMBOL")

    def load():
        r = _get("https://api.binance.com/api/v3/klines",
                 {"symbol": sym, "interval": tf["binance"], "limit": min(tf["points"], 1000)})
        if r.status_code != 200:
            raise FeedError(f"Binance {r.status_code} for {sym}")
        rows = r.json()
        if not isinstance(rows, list) or len(rows) < 2:
            raise FeedError("Binance returned no data")
        candles = [{"time": _iso(x[0] / 1000), "open": float(x[1]), "high": float(x[2]), "low": float(x[3]),
                    "close": float(x[4])} for x in rows]
        return {"candles": candles, "price": candles[-1]["close"], "source": "binance"}
    return CACHE.get(f"bn:{sym}:{tf['binance']}", 120, load)


def broker_candles(broker, tf: dict) -> dict:
    """The trading terminal's own candles (MT5 only; the simulator has none)."""
    fn = getattr(broker, "candles", None)
    if fn is None:
        raise FeedError("NO_BROKER_CANDLES")

    def load():
        candles = fn(tf["minutes"], tf["points"])
        if len(candles) < 30:
            raise FeedError(f"Broker series too short ({len(candles)})")
        q = broker.quote()
        return {"candles": candles, "price": q.bid if q else candles[-1]["close"], "source": "mt5"}
    return CACHE.get(f"mt5:{tf['id']}", 60, load)


# ------------------------------------------------------------------ demo

BASE_PRICE = {"gold": 2350, "silver": 28, "oil": 78, "eurusd": 1.085, "gbpusd": 1.27, "usdjpy": 156,
              "bitcoin": 68000, "ethereum": 3500}
VOL = {"5m": 0.0015, "15m": 0.002, "1h": 0.004, "4h": 0.008, "1d": 0.012}


def _round(n: float) -> float:
    return round(n, 2 if n >= 1000 else 4 if n >= 1 else 6)


def demo(asset: dict, tf: dict) -> dict:
    """Synthetic but realistic-looking history, so the dashboard works before any keys are set."""
    rng = random.Random(f"{asset['id']}|{tf['id']}|v2")
    base = BASE_PRICE.get(asset["id"], 100)
    vol = base * VOL.get(tf["id"], 0.012)
    price = base * (0.9 + rng.random() * 0.2)
    drift = (rng.random() - 0.5) * vol * 0.15
    now, bar = time.time(), tf["minutes"] * 60
    candles = []
    for i in range(tf["points"] - 1, -1, -1):
        drift = max(-vol * 0.4, min(vol * 0.4, drift + (rng.random() - 0.5) * vol * 0.05))
        o = price
        c = max(o + drift + (rng.random() - 0.5) * vol * 2, base * 0.3)
        h = max(o, c) + rng.random() * vol
        lo = min(o, c) - rng.random() * vol
        candles.append({"time": _iso(now - i * bar), "open": _round(o), "high": _round(h), "low": _round(lo), "close": _round(c)})
        price = c
    return {"candles": candles, "price": candles[-1]["close"], "source": "demo"}


def demo_news(asset: dict) -> list[dict]:
    rng = random.Random(asset["id"] + "|news")
    n, a = asset["name"], asset["name_ar"]
    rows = [
        (f"{n} steadies as traders weigh rate-path expectations", f"استقرار {a} مع ترقب المتداولين لمسار أسعار الفائدة", 0.05),
        (f"Analysts flag {n} volatility ahead of key data release", f"محللون يحذرون من تذبذب {a} قبل صدور بيانات اقتصادية مهمة", -0.15),
        (f"{n} demand outlook brightens on improving sentiment", f"تحسّن توقعات الطلب على {a} مع تعافي المعنويات", 0.35),
        (f"Profit-taking pressures {n} after recent run-up", f"جني الأرباح يضغط على {a} بعد صعود أخير", -0.3),
        (f"{n} holds range as markets await macro catalysts", f"{a} يلتزم نطاقه مع انتظار الأسواق محفزات اقتصادية", 0.0),
    ]
    out = [{"title": t, "title_ar": ta, "source": "Demo Newswire", "url": "", "published_at": _iso(time.time() - i * 5 * 3600),
            "sentiment": s} for i, (t, ta, s) in enumerate(rows)]
    return out[:4 - int(rng.random() * 2)]


# ------------------------------------------------------------------ news

NEWS_SYMBOLS = {"gold": "XAUUSD,GLD", "silver": "XAGUSD,SLV", "oil": "CLUSD,USO", "eurusd": "EURUSD",
                "gbpusd": "GBPUSD", "usdjpy": "USDJPY", "bitcoin": "BTCUSD,BTC", "ethereum": "ETHUSD,ETH"}
NEWS_SEARCH = {"gold": "gold", "silver": "silver", "oil": "crude oil", "eurusd": "EUR USD", "gbpusd": "GBP USD",
               "usdjpy": "USD JPY", "bitcoin": "bitcoin", "ethereum": "ethereum"}


def news(asset: dict, key: str) -> dict:
    """Marketaux headlines with per-article sentiment; demo headlines without a key."""
    if not key:
        return {"demo": True, "articles": demo_news(asset)}

    def fetch(params: dict) -> list[dict]:
        r = _get("https://api.marketaux.com/v1/news/all", {"api_token": key, "language": "en", "limit": 5, **params})
        j = r.json() if r.content else {}
        if r.status_code != 200:
            err = j.get("error") or {}
            raise FeedError(f"Marketaux {err.get('code', r.status_code)}: {err.get('message', '')}")
        return j.get("data") or []

    def load():
        data = fetch({"symbols": NEWS_SYMBOLS.get(asset["id"], ""), "filter_entities": "true", "must_have_entities": "true"})
        if not data:
            data = fetch({"search": NEWS_SEARCH.get(asset["id"], asset["name"])})
        return {"demo": False, "articles": [_article(a) for a in data]}
    return CACHE.get(f"news:{asset['id']}", 900, load)


def _article(a: dict) -> dict:
    scores = [e["sentiment_score"] for e in a.get("entities") or [] if isinstance(e.get("sentiment_score"), (int, float))]
    return {"title": a.get("title"), "source": a.get("source"), "url": a.get("url"),
            "published_at": a.get("published_at"), "sentiment": sum(scores) / len(scores) if scores else 0.0}


# -------------------------------------------------------------- calendar

CAL_URLS = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            "https://nfs.faireconomy.media/ff_calendar_nextweek.json"]
ASSET_CURRENCIES = {"gold": ["USD"], "silver": ["USD"], "oil": ["USD"], "eurusd": ["EUR", "USD"],
                    "gbpusd": ["GBP", "USD"], "usdjpy": ["JPY", "USD"], "bitcoin": ["USD"], "ethereum": ["USD"]}


def calendar() -> list[dict]:
    """Forex Factory events for this and next week: [{title, currency, impact, time (epoch s)}]."""
    def load():
        events = []
        for url in CAL_URLS:
            try:
                r = _get(url, timeout=10)
                if r.status_code != 200:
                    continue
                for e in r.json() or []:
                    try:
                        ts = datetime.fromisoformat(e["date"]).timestamp()
                    except (KeyError, ValueError):
                        continue
                    events.append({"title": e.get("title", ""), "currency": (e.get("country") or "").upper(),
                                   "impact": (e.get("impact") or "").lower(), "time": ts})
            except Exception:   # one feed failing is fine
                continue
        events.sort(key=lambda e: e["time"])
        return {"events": events}
    return CACHE.get("calendar", 4 * 3600, load)["events"]


def news_blackout(asset_id: str, cfg, now: float | None = None) -> dict | None:
    """The high-impact event whose blackout window we're in, or None. Fails open."""
    if not cfg.news_filter:
        return None
    try:
        events = calendar()
    except Exception as ex:
        log.warning("calendar unavailable, trading without the news filter: %s", ex)
        return None
    now = now or time.time()
    cur = ASSET_CURRENCIES.get(asset_id, ["USD"])
    before, after = cfg.news_before_min * 60, cfg.news_after_min * 60
    for e in events:
        if e["impact"] not in cfg.news_impacts or e["currency"] not in cur:
            continue
        if e["time"] - before <= now <= e["time"] + after:
            return e
        if e["time"] - before > now:
            break
    return None


def upcoming(cfg, hours: float = 48) -> list[dict]:
    try:
        events = calendar()
    except Exception:
        return []
    now = time.time()
    return [{"title": e["title"], "currency": e["currency"], "impact": e["impact"], "time": e["time"],
             "in_minutes": round((e["time"] - now) / 60)}
            for e in events if e["impact"] in cfg.news_impacts
            and now - cfg.news_after_min * 60 <= e["time"] <= now + hours * 3600]
