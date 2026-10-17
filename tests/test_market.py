import random

import pytest
from fastapi.testclient import TestClient

from engine import SqliteStore
from engine.sim_broker import SimBroker
from market import MarketConfig, MarketService
from market.indicators import compute, rsi, sma_series, support_resistance
from market.providers import aggregate
from server import TradingService, create_app
from signals import Channel, SignalService, feed_channel_id

TOKEN = "t"
BID = 2650.00


def candles(n=200, start=100.0, drift=0.0, seed=1):
    r, p, out = random.Random(seed), start, []
    for i in range(n):
        o = p
        c = o + drift + r.gauss(0, start * 0.002)
        out.append({"time": f"t{i}", "open": o, "high": max(o, c) + abs(r.gauss(0, start * 0.001)),
                    "low": min(o, c) - abs(r.gauss(0, start * 0.001)), "close": c})
        p = c
    return out


class FakeFeed:
    """Deterministic prices per asset; gold's direction can be switched between refreshes."""

    def __init__(self):
        self.gold_drift = 1.5

    def __call__(self, asset, tf):
        if asset["id"] == "gold":
            cs = candles(start=BID, drift=self.gold_drift, seed=3)
            shift = BID - cs[-1]["close"]              # end exactly at the broker price
            cs = [{**c, **{k: c[k] + shift for k in ("open", "high", "low", "close")}} for c in cs]
            return {"candles": cs, "price": cs[-1]["close"], "source": "mt5"}, False
        if asset["id"] == "oil":
            cs = candles(seed=9)
            return {"candles": cs, "price": cs[-1]["close"], "source": "demo"}, True
        cs = candles(start=50 + len(asset["id"]), seed=len(asset["id"]))
        return {"candles": cs, "price": cs[-1]["close"], "source": "yahoo"}, False


@pytest.fixture
def env(tmp_path):
    broker = SimBroker(bid=BID)
    trading = TradingService(broker, SqliteStore(tmp_path / "m.db"), sim_walk=False)
    sigs = SignalService(trading, None)
    cfg = MarketConfig(news_filter=False, feed_tp_rs=[1, 2, 3], feed_min_confidence=0, feed_min_score=0)
    feed = FakeFeed()
    mkt = MarketService(cfg, trading, sigs, fetch=feed)
    app = create_app(trading, token=TOKEN, run_loop=False, signals=sigs, market=mkt)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        yield client, trading, sigs, mkt, feed


def test_indicator_basics():
    assert sma_series([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    assert rsi([float(i) for i in range(30)]) == 100.0
    flat = [{"time": i, "open": 1, "high": 2, "low": 0.5, "close": 1} for i in range(4)]
    assert support_resistance(flat)["method"] == "range"
    assert aggregate([{"time": i, "open": i, "high": i + 1, "low": i - 1, "close": i} for i in range(8)], 4)[1] == \
        {"time": 4, "open": 4, "high": 8, "low": 3, "close": 7}
    ind = compute(candles())
    assert ind["sma50"] and ind["levels"]["support"] < ind["price"] < ind["levels"]["resistance"]


def test_snapshot_and_api(env):
    client, trading, sigs, mkt, feed = env
    r = client.get("/api/market?tf=4h")
    assert r.status_code == 200, r.text
    snap = r.json()
    assert snap["status"] == "ok" and len(snap["assets"]) == 8 and snap["timeframe"]["id"] == "4h"
    gold = next(a for a in snap["assets"] if a["id"] == "gold")
    assert gold["source"] == "mt5" and gold["price"] == BID and len(gold["chart"]["close"]) == 80
    assert -100 <= gold["signal"]["composite"] <= 100 and gold["signal"]["summary"]["ar"]
    assert next(a for a in snap["assets"] if a["id"] == "oil")["demo_price"] is True
    assert snap["calibration"]["active"] is True and trading.store.get_setting("market_weights")
    assert set(snap["triage"]) == {"bullish", "bearish", "movers", "buys", "sells"}
    assert client.get("/api/market/assets/silver?tf=4h").json()["id"] == "silver"
    assert client.get("/api/market/assets/nope").status_code == 404
    assert client.post("/api/market/refresh?tf=1h").json()["status"] == "ok"
    assert client.post("/api/market/refresh?tf=1h").status_code == 429   # rate limited
    assert client.get("/api/market", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_gold_idea_becomes_a_managed_channel_trade(env):
    client, trading, sigs, mkt, feed = env
    snap = mkt.refresh("4h")
    tr = next(a for a in snap["assets"] if a["id"] == "gold")["signal"]["trade"]
    assert tr["active"] and tr["side"] == "buy", tr
    s = client.get("/api/signals").json()
    assert len(s) == 1 and s[0]["status"] == "pending" and s[0]["channel_title"] == "Market Watch"
    assert s[0]["parsed"]["sl"] == tr["stop"] and len(s[0]["parsed"]["tps"]) == 3       # 1R/2R/3R -> P1-P3
    assert mkt.feed.status()["holding"]["side"] == "buy"
    mkt.refresh("4h")                                                                   # same idea: nothing new
    assert len(client.get("/api/signals").json()) == 1

    # Trust it: auto mode places the next idea and the engine manages it.
    sigs.save_channel(Channel(feed_channel_id("market-watch"), "Market Watch", weight=2, mode="auto"))
    feed.gold_drift = -3.0
    snap = mkt.refresh("4h")
    assert next(a for a in snap["assets"] if a["id"] == "gold")["signal"]["trade"]["side"] == "sell"
    s = client.get("/api/signals").json()
    assert [x["status"] for x in s] == ["dismissed", "ignored", "placed"]   # buy withdrawn, close had no trade, sell placed
    t = trading.store.trades[s[-1]["ticket"]]
    assert t.side.value == "sell" and t.settings.lot == 0.02 and t.open
    assert [a["type"] for a in mkt.feed.status()["actions"][:3]] == ["open", "close", "open"]


def test_flip_alerts_and_market_off(env, tmp_path):
    client, trading, sigs, mkt, feed = env
    mkt.refresh("4h")
    feed.gold_drift = -3.0
    snap = mkt.refresh("4h")
    assert any(f["id"] == "gold" for f in snap["new_flips"]) and snap["flips"]
    assert any("Gold (4h)" in e["message"] for e in trading.store.recent_events(20))
    plain = TradingService(SimBroker(), SqliteStore(tmp_path / "x.db"), sim_walk=False)
    with TestClient(create_app(plain, token=TOKEN, run_loop=False),
                    headers={"Authorization": f"Bearer {TOKEN}"}) as c2:
        assert c2.get("/api/market").status_code == 503
