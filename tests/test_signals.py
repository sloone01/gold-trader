import pytest
from fastapi.testclient import TestClient

from engine import SqliteStore
from engine.sim_broker import SimBroker
from server import TradingService, create_app
from signals import Channel, ParsedSignal, SignalService, SignalSettings
from signals.models import Signal

TOKEN = "t"
CH = -1001
BID = 2650.00   # ask 2650.30


class FakeParser:
    """Deterministic stand-in for Claude: recognises a few message shapes."""

    def __call__(self, text: str, context: str = "") -> ParsedSignal:
        t = text.lower()
        if "btc" in t:
            return ParsedSignal(action="ignore", symbol="BTCUSD", reason="not gold")
        if t.startswith("close half"):
            return ParsedSignal(action="close_partial", close_pct=50, confidence=0.9)
        if t.startswith("close"):
            return ParsedSignal(action="close", confidence=0.9)
        if "breakeven" in t or "sl to entry" in t:
            return ParsedSignal(action="move_sl", move_sl_to="breakeven", confidence=0.9)
        if t.startswith("tp") or t.endswith("+") or t.startswith("sl hit"):
            if t.startswith("sl hit"):
                return ParsedSignal(action="result", result="sl", result_pips=50, confidence=0.9)
            nums = [float(x.strip("+")) for x in t.split() if x.strip("+").replace(".", "").isdigit()]
            n = nums[-1] if nums else None
            return ParsedSignal(action="result", result="tp" if t.startswith("tp") else "pips", result_pips=n,
                                result_tp=1 if t.startswith("tp1") else None, confidence=0.9)
        if t.startswith("watch"):
            return ParsedSignal(action="watch", symbol="XAUUSD", side="buy", entry=2660, reason="buy if it breaks 2660", confidence=0.8)
        if t.startswith("gold buy") or t.startswith("gold sell"):
            side = "buy" if "buy" in t else "sell"
            nums = [float(x) for x in t.replace("tp", " ").replace("sl", " ").replace(":", " ").split() if x.replace(".", "").isdigit()]
            if "no sl" in t:
                return ParsedSignal(action="open", symbol="GOLD", side=side, entry=nums[0], sl=None, tps=nums[1:], confidence=0.9)
            entry, sl, tps = nums[0], nums[1], nums[2:]
            return ParsedSignal(action="open", symbol="GOLD", side=side, entry=entry, sl=sl, tps=tps, confidence=0.95)
        return ParsedSignal(action="ignore", reason="chat")


@pytest.fixture
def env(tmp_path):
    broker = SimBroker(bid=BID)
    trading = TradingService(broker, SqliteStore(tmp_path / "s.db"), sim_walk=False)
    sigs = SignalService(trading, FakeParser())
    app = create_app(trading, token=TOKEN, run_loop=False, signals=sigs)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        yield client, trading, sigs, broker


def walk(trading, broker, bids):
    out = []
    for b in bids:
        broker.set_price(b)
        out += trading.tick()
    return out


def test_channel_crud_and_weight_lot(env):
    client, trading, sigs, broker = env
    r = client.put(f"/api/channels/{CH}", json={"title": "Gold VIP", "weight": 2, "mode": "confirm"})
    assert r.status_code == 200 and r.json()["weight"] == 2
    assert client.get("/api/channels").json()[0]["review"]["signals"] == 0
    # weight 2 -> 0.02 lot
    sig = sigs.handle_message(CH, "Gold VIP", 1, "GOLD BUY 2650 SL 2645 TP 2655 TP 2660 TP 2670")
    assert sig.status == "pending" and sig.plan["lot"] == 0.02
    assert sig.plan["settings"]["stop_mode"] == "far" and sig.plan["settings"]["sl_pips"] == 53.0  # from ask 2650.30
    # three TPs become P1-P3 distances, preset keeps its close % and SL moves
    assert [s["pips"] for s in sig.plan["settings"]["steps"]] == [47.0, 97.0, 197.0]
    assert [s["close_pct"] for s in sig.plan["settings"]["steps"]] == [50, 25, 0]


def test_confirm_then_place_and_followups(env):
    client, trading, sigs, broker = env
    sigs.save_channel(Channel(CH, "Gold VIP", weight=5, mode="confirm"))
    sig = sigs.handle_message(CH, "Gold VIP", 1, "GOLD SELL 2650 SL 2660 TP 2640")
    assert sig.status == "pending" and sig.plan["lot"] == 0.05
    assert len([s for s in sig.plan["settings"]["steps"]]) == 3  # preset steps (only one TP)
    r = client.post(f"/api/signals/{sig.id}/place", json={"lot": 0.02})
    assert r.status_code == 200, r.text
    placed = r.json()
    assert placed["status"] == "placed" and placed["ticket"]
    t = trading.get_trade(placed["ticket"])
    assert t["side"] == "sell" and t["volume"] == 0.02 and t["sl"] == 2660.0

    # Follow-up: breakeven request waits for confirmation, then applies.
    be = sigs.handle_message(CH, "Gold VIP", 2, "move SL to entry")
    assert be.status == "pending" and be.plan["action"] == "move_sl" and be.ticket == placed["ticket"]
    # SL to +1 pip is too close to price right now (sell at 2650.00, price 2650.30) -> engine rejects
    r = client.post(f"/api/signals/{be.id}/place")
    assert r.json()["status"] == "failed" and "too close" in r.json()["error"]
    # Move price into profit, then close half auto-applies in auto mode.
    sigs.save_channel(Channel(CH, "Gold VIP", weight=5, mode="auto"))
    walk(trading, broker, [2648.00])                     # +17 pips: P1 (20) not reached yet
    half = sigs.handle_message(CH, "Gold VIP", 3, "close half")
    assert half.status == "applied"
    assert broker.position(placed["ticket"]).volume == 0.01
    full = sigs.handle_message(CH, "Gold VIP", 4, "close now")
    assert full.status == "applied" and broker.position(placed["ticket"]) is None


def test_auto_mode_trades_at_any_weight(env):
    client, trading, sigs, broker = env
    sigs.save_channel(Channel(CH, "Gold VIP", weight=3, mode="auto"))
    sig = sigs.handle_message(CH, "Gold VIP", 1, "GOLD BUY 2650 SL 2645")
    assert sig.status == "placed" and trading.store.trades[sig.ticket].settings.lot == 0.03  # weight 3
    sigs.save_channel(Channel(CH, "Gold VIP", weight=1, mode="auto"))
    sig = sigs.handle_message(CH, "Gold VIP", 2, "GOLD BUY 2650 SL 2645")
    assert sig.status == "placed" and trading.store.trades[sig.ticket].settings.lot == 0.01  # weight 1


def test_ignores_and_errors(env):
    client, trading, sigs, broker = env
    sigs.save_channel(Channel(CH, "Gold VIP", weight=5, mode="auto"))
    assert sigs.handle_message(CH, "Gold VIP", 1, "BTC buy 60000 sl 59000").status == "ignored"
    assert sigs.handle_message(CH, "Gold VIP", 2, "good morning traders").status == "ignored"
    bad = sigs.handle_message(CH, "Gold VIP", 3, "GOLD BUY 2650 SL 2660")   # SL above entry on a buy
    assert bad.status == "failed" and "wrong side" in bad.error
    assert sigs.handle_message(-999, "Unknown", 1, "GOLD BUY 2650 SL 2645") is None
    assert sigs.handle_message(CH, "Gold VIP", 4, "close now").status == "ignored"   # nothing open


def test_review_scorecard_and_test_endpoint(env):
    client, trading, sigs, broker = env
    sigs.save_channel(Channel(CH, "Gold VIP", weight=5, mode="auto"))
    sig = sigs.handle_message(CH, "Gold VIP", 1, "GOLD BUY 2650 SL 2645 TP 2655 TP 2660 TP 2670")
    assert sig.status == "placed"
    walk(trading, broker, [2680.00, 2660.00, 2640.00])  # P1-P3 hit, then stopped out in profit
    rev = client.get(f"/api/channels/{CH}/review").json()
    assert rev["placed"] == 1 and rev["closed"] == 1 and rev["wins"] == 1 and rev["result_usd"] > 0
    r = client.post("/api/signals/test", json={"text": "GOLD SELL 2650 SL 2655"})
    assert r.status_code == 200 and r.json()["plan"]["side"] == "sell" and r.json()["parsed"]["symbol"] == "XAUUSD"
    assert client.get("/api/signals").json()[0]["status"] == "placed"


def test_signals_disabled_returns_503(tmp_path):
    trading = TradingService(SimBroker(), SqliteStore(tmp_path / "x.db"), sim_walk=False)
    app = create_app(trading, token=TOKEN, run_loop=False)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        assert client.get("/api/signals/status").json()["enabled"] is False
        assert client.get("/api/channels").status_code == 503


def test_topic_filter(env):
    client, trading, sigs, broker = env
    r = client.put(f"/api/channels/{CH}", json={"title": "VIP Group", "weight": 5, "mode": "confirm",
                                                "topics": [{"id": 77, "title": "Gold signals"}, {"id": 4, "title": "Medium risk"}]})
    assert r.status_code == 200 and [t["id"] for t in r.json()["topics"]] == [77, 4]
    assert sigs.handle_message(CH, "VIP Group", 1, "GOLD BUY 2650 SL 2645", topic_id=12) is None   # other topic
    assert sigs.handle_message(CH, "VIP Group", 2, "GOLD BUY 2650 SL 2645", topic_id=None) is None  # general
    sig = sigs.handle_message(CH, "VIP Group", 3, "GOLD BUY 2650 SL 2645", topic_id=77)
    assert sig.status == "pending" and sig.channel_title == "VIP Group › Gold signals"
    sig = sigs.handle_message(CH, "VIP Group", 4, "GOLD BUY 2650 SL 2645", topic_id=4)
    assert sig.status == "pending" and sig.channel_title == "VIP Group › Medium risk"
    # a channel saved by the old single-topic version still loads
    from signals import Channel
    old = Channel.from_dict({"id": 5, "title": "Old", "weight": 3, "mode": "confirm", "preset_id": None, "topic_id": 9, "topic_title": "T"})
    assert old.topic_ids() == {9} and old.topic_title(9) == "T"


def test_missing_sl_falls_back_to_preset_and_watch_is_recorded(env):
    client, trading, sigs, broker = env
    sigs.save_channel(Channel(CH, "Gold VIP", weight=5, mode="auto"))
    sig = sigs.handle_message(CH, "Gold VIP", 1, "GOLD BUY no sl 2650 TP 2660")
    assert sig.status == "placed", sig.error
    t = trading.store.trades[sig.ticket]
    assert t.settings.sl_pips == 50.0 and t.sl == 2645.30            # default 50 pips until the channel gives a stop
    assert any("No stop loss in the signal" in w for w in sig.plan["warnings"])
    w = sigs.handle_message(CH, "Gold VIP", 2, "watch 2660 breakout")
    assert w.status == "watch" and "breaks 2660" in w.error
    assert [s["status"] for s in client.get("/api/signals").json()] == ["placed", "watch"]


def test_followup_tp_under_p3_squeezes_pending_steps(env):
    client, trading, sigs, broker = env
    sigs.save_channel(Channel(CH, "Gold VIP", weight=5, mode="auto"))
    sig = sigs.handle_message(CH, "Gold VIP", 1, "GOLD BUY 2650 SL 2645")     # preset steps +20/+50/+100
    assert sig.status == "placed"

    class TPParser:
        def __call__(self, text, context="", **kw):
            return ParsedSignal(action="modify_tp", tps=[2655.0], confidence=0.9)   # +47 pips from 2650.30
    sigs.parser = TPParser()
    f = sigs.handle_message(CH, "Gold VIP", 2, "take profit at 2655")
    assert f.status == "applied", f.error
    t = trading.store.trades[sig.ticket]
    assert t.tp == 2655.0
    pips = [s.pips for s in t.settings.steps]
    assert pips[0] < pips[1] < pips[2] < 47.0


def test_channel_claims_feed_the_scorecard(env):
    client, trading, sigs, broker = env
    sigs.save_channel(Channel(CH, "Gold VIP", weight=3, mode="confirm"))
    for i, msg in enumerate(["TP1 hit 40", "60+", "sl hit", "TP2 hit"], 1):
        assert sigs.handle_message(CH, "Gold VIP", i, msg).status == "result"
    rev = client.get(f"/api/channels/{CH}/review").json()
    assert rev["claims"] == 4 and rev["claimed_tp"] == 2 and rev["claimed_sl"] == 1
    assert rev["claimed_pips"] == 40 + 60 - 50 and rev["claimed_rate"] == 67
    assert rev["placed"] == 0
