"""Breakeven armor, reply targeting, waiting for the stop, risk sizing, entry ladders, limit orders,
own signals and the timeline."""
import time

import pytest
from fastapi.testclient import TestClient

from engine import SqliteStore
from engine.sim_broker import SimBroker
from server import TradingService, create_app
from signals import Channel, ParsedSignal, SignalService

TOKEN = "t"
CH = -1001
BID = 2650.00   # ask 2650.30


class ScriptParser:
    """Returns the ParsedSignal registered for a message text."""

    def __init__(self):
        self.map = {}

    def __call__(self, text, context="", **kw):
        return self.map.get(text, ParsedSignal(action="ignore", reason="chat"))


@pytest.fixture
def env(tmp_path):
    broker = SimBroker(bid=BID)
    trading = TradingService(broker, SqliteStore(tmp_path / "c.db"), sim_walk=False)
    parser = ScriptParser()
    sigs = SignalService(trading, parser)
    sigs.save_channel(Channel(CH, "Gold VIP", weight=3, mode="auto"))
    app = create_app(trading, token=TOKEN, run_loop=False, signals=sigs)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        yield client, trading, sigs, broker, parser.map


def buy(entry=2650.0, sl=2645.0, tps=(2655, 2660, 2670), **kw):
    return ParsedSignal(action="open", symbol="XAUUSD", side="buy", entry=entry, sl=sl, tps=list(tps), confidence=0.9, **kw)


def walk(trading, broker, *bids):
    for b in bids:
        broker.set_price(b)
        trading.tick()


def test_breakeven_armor_on_channel_tp_hit(env):
    client, trading, sigs, broker, m = env
    m["buy"] = buy()
    m["TP1 hit"] = ParsedSignal(action="result", result="tp", result_tp=1, confidence=0.9)
    sig = sigs.handle_message(CH, "Gold VIP", 1, "buy")
    assert sig.status == "placed"
    walk(trading, broker, 2653.0)                         # +27 pips: before our P1 (47 pips)
    res = sigs.handle_message(CH, "Gold VIP", 2, "TP1 hit")
    assert res.status == "result" and "breakeven" in res.note
    t = trading.store.trades[sig.ticket]
    assert t.sl == pytest.approx(t.entry + 0.1)           # entry + 1 pip buffer
    assert client.get(f"/api/channels/{CH}/review").json()["claimed_tp"] == 1   # still counted as a claim
    sigs.cfg.be_on_tp_hit = False
    assert sigs.handle_message(CH, "Gold VIP", 3, "TP1 hit").note is None


def test_followups_follow_the_reply(env):
    client, trading, sigs, broker, m = env
    m["buy 1"], m["buy 2"] = buy(), buy()
    m["close"] = ParsedSignal(action="close", confidence=0.9)
    a = sigs.handle_message(CH, "Gold VIP", 10, "buy 1")
    b = sigs.handle_message(CH, "Gold VIP", 11, "buy 2")
    c = sigs.handle_message(CH, "Gold VIP", 12, "close", reply_to=10)   # replies to the first signal
    assert c.status == "applied" and c.plan["tickets"] == [a.ticket]
    assert broker.position(a.ticket) is None and broker.position(b.ticket) is not None
    d = sigs.handle_message(CH, "Gold VIP", 13, "close")                # no reply: the latest open signal
    assert d.plan["tickets"] == [b.ticket] and broker.position(b.ticket) is None


def test_waits_for_the_stop_then_enters(env):
    client, trading, sigs, broker, m = env
    m["buy now"] = buy(sl=None)
    m["sl 2640"] = ParsedSignal(action="move_sl", move_sl_to="2640", confidence=0.9)
    sig = sigs.handle_message(CH, "Gold VIP", 1, "buy now")
    assert sig.status == "waiting_sl" and trading.store.open_trades() == []
    follow = sigs.handle_message(CH, "Gold VIP", 2, "sl 2640")
    assert follow.status == "applied" and "Completed signal" in follow.note
    sig = sigs._get(sig.id)
    assert sig.status == "placed" and trading.store.trades[sig.ticket].sl == 2640.0
    # skip mode: no stop in time -> no trade
    sigs.cfg.no_sl_action, sigs.cfg.wait_for_sl_s = "skip", 0.01
    s2 = sigs.handle_message(CH, "Gold VIP", 3, "buy now")
    time.sleep(0.02)
    sigs.tick()
    assert sigs._get(s2.id).status == "expired" and len(trading.store.open_trades()) == 1


def test_risk_sizing_from_the_stop(env):
    client, trading, sigs, broker, m = env
    sigs.cfg.sizing, sigs.cfg.risk_pct_per_weight = "risk", 0.25
    sigs.save_channel(Channel(CH, "Gold VIP", weight=2, mode="confirm"))
    m["tight"] = buy(sl=2645.30)                          # 50 pips from ask: $5 per 0.01 lot
    m["wide"] = buy(sl=2640.30)                           # 100 pips
    tight = sigs.handle_message(CH, "Gold VIP", 1, "tight")
    assert tight.plan["lot"] == 0.1 and tight.plan["risk_usd"] == 50.0     # 0.5% of $10,000
    wide = sigs.handle_message(CH, "Gold VIP", 2, "wide")
    assert wide.plan["lot"] == 0.05 and wide.plan["risk_usd"] == 50.0      # same $ risk, half the lot


def test_ladder_over_a_zone(env):
    client, trading, sigs, broker, m = env
    sigs.cfg.ladder = True
    sigs.save_channel(Channel(CH, "Gold VIP", weight=3, mode="auto"))     # 0.03 lot -> 3 x 0.01
    m["zone"] = buy(entry=2640.0, entry_high=2650.0, sl=2630.0, tps=(2660, 2670, 2680))
    m["close"] = ParsedSignal(action="close", confidence=0.9)
    sig = sigs.handle_message(CH, "Gold VIP", 1, "zone")
    assert [(l["kind"], l["price"], l["status"]) for l in sig.plan["legs"]] == [
        ("market", 2650.0, "placed"), ("limit", 2645.0, "waiting"), ("limit", 2640.0, "waiting")]
    assert sig.status == "placed" and len(sig.tickets) == 1 and "1 of 3" in sig.note
    walk(trading, broker, 2644.5)                          # ask 2644.80: second edge reached
    sigs.tick()
    sig = sigs._get(sig.id)
    assert len(sig.tickets) == 2 and trading.store.trades[sig.tickets[1]].volume == 0.01
    out = sigs.handle_message(CH, "Gold VIP", 2, "close")  # closes both legs, cancels the third
    assert sorted(out.plan["tickets"]) == sorted(sig.tickets)
    assert all(broker.position(t) is None for t in sig.tickets)
    assert [l["status"] for l in sigs._get(sig.id).plan["legs"]][2] == "cancelled"


def test_limit_order_waits_then_fills_or_expires(env):
    client, trading, sigs, broker, m = env
    m["limit"] = buy(entry=2640.0, sl=2630.0, tps=(2650, 2660, 2670), order_type="limit")
    sig = sigs.handle_message(CH, "Gold VIP", 1, "limit")
    assert sig.status == "armed" and sig.tickets == []
    sigs.tick()
    assert sigs._get(sig.id).status == "armed"             # not there yet
    walk(trading, broker, 2639.5)
    sigs.tick()
    sig = sigs._get(sig.id)
    assert sig.status == "placed" and trading.store.trades[sig.ticket].sl == 2630.0
    m["limit2"] = buy(entry=2600.0, sl=2590.0, order_type="limit")
    s2 = sigs.handle_message(CH, "Gold VIP", 2, "limit2")
    s2.plan["expires_at"] = time.time() - 1
    sigs._save(s2)
    sigs.tick()
    assert sigs._get(s2.id).status == "expired"


def test_stop_hit_before_entry_cancels(env):
    client, trading, sigs, broker, m = env
    m["limit"] = buy(entry=2640.0, sl=2635.0, order_type="limit")
    sig = sigs.handle_message(CH, "Gold VIP", 1, "limit")
    walk(trading, broker, 2634.0)                          # gaps through entry and stop
    sigs.tick()
    assert sigs._get(sig.id).status == "expired" and trading.store.open_trades() == []


def test_own_signals_and_timeline(env):
    client, trading, sigs, broker, m = env
    m["my buy"] = buy()
    m["be"] = ParsedSignal(action="move_sl", move_sl_to="breakeven", confidence=0.9)
    r = client.post("/api/signals/own", json={"text": "my buy"})
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["channel_title"] == "My signals"
    sid = r.json()["id"]
    placed = client.post(f"/api/signals/{sid}/place").json()
    assert placed["status"] == "placed"
    walk(trading, broker, 2653.0)                          # in profit, before P1
    sigs.save_channel(Channel(sigs.channel(placed["channel_id"]).id, "My signals", mode="auto"))
    assert sigs.handle_message(placed["channel_id"], "My signals", 99, "be").status == "applied"
    tl = client.get(f"/api/signals/{sid}/timeline").json()
    assert tl["signal"]["id"] == sid and [f["status"] for f in tl["followups"]] == ["applied"]
    assert tl["trades"][0]["ticket"] == placed["ticket"] and any("SL moved" in e["message"] for e in tl["events"])


def test_master_pause_stops_automatic_trading(env):
    client, trading, sigs, broker, m = env
    m["buy"] = buy()
    m["limit"] = buy(entry=2640.0, sl=2630.0, order_type="limit")
    assert client.put("/api/automation", json={"paused": True}).json() == {"paused": True}
    assert client.get("/api/status").json()["automation_paused"] is True
    sig = sigs.handle_message(CH, "Gold VIP", 1, "buy")              # auto channel, but paused
    assert sig.status == "pending" and trading.store.open_trades() == []
    client.put("/api/automation", json={"paused": False})
    assert sigs.handle_message(CH, "Gold VIP", 2, "limit").status == "armed"
    client.put("/api/automation", json={"paused": True})
    walk(trading, broker, 2639.0)
    sigs.tick()                                                       # entry price reached, but paused
    assert trading.store.open_trades() == []
    client.put("/api/automation", json={"paused": False})
    sigs.tick()
    assert len(trading.store.open_trades()) == 1
