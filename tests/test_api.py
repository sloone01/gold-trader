import pytest
from fastapi.testclient import TestClient

from engine import Side, SqliteStore, TradeSettings
from engine.sim_broker import SimBroker
from server import TradingService, create_app

TOKEN = "test-token"


@pytest.fixture
def env(tmp_path):
    broker = SimBroker(bid=2650.00)
    service = TradingService(broker, SqliteStore(tmp_path / "t.db"), sim_walk=False)
    app = create_app(service, token=TOKEN, run_loop=False)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        yield client, service, broker


def walk(service, broker, bids):
    out = []
    for b in bids:
        broker.set_price(b)
        out += service.tick()
    return out


def test_auth_required(env):
    client, *_ = env
    assert client.get("/api/status", headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/status").status_code == 200


def test_status_and_presets(env):
    client, *_ = env
    st = client.get("/api/status").json()
    assert st["quote"]["bid"] == 2650.0 and st["connected"] and st["open_trades"] == []
    names = [p["name"] for p in client.get("/api/presets").json()]
    assert "Default hybrid" in names


def test_open_modify_close_flow(env):
    client, service, broker = env
    body = {"side": "buy", "settings": {"lot": 0.04}}
    pv = client.post("/api/trades/preview", json=body).json()
    assert pv["entry"] == 2650.30 and pv["errors"] == []

    r = client.post("/api/trades", json=body)
    assert r.status_code == 201, r.text
    t = r.json()
    ticket = t["ticket"]
    assert t["sl_pips"] == -50 and t["current_volume"] == 0.04

    # Move SL to -30 pips and set a TP at +200, then edit P3 before it fires.
    r = client.patch(f"/api/trades/{ticket}", json={"sl_pips": -30, "tp_pips": 200,
                                                    "steps": {"2": {"pips": 120, "move_sl_pips": 60}}})
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["sl_pips"] == -30 and t["tp_pips"] == 200 and t["settings"]["steps"][2]["pips"] == 120
    assert broker.position(ticket).sl == 2647.30

    # Hit P1, then a step that was already hit can't be edited.
    ev = walk(service, broker, [2652.30])
    assert ev[0].kind == "step_hit"
    r = client.patch(f"/api/trades/{ticket}", json={"steps": {"0": {"pips": 25}}})
    assert r.status_code == 400 and "already hit" in r.json()["detail"]

    # Partial close of 0.01, then close the rest.
    assert client.post(f"/api/trades/{ticket}/close", json={"volume": 0.01}).status_code == 200
    assert broker.position(ticket).volume == 0.01
    assert client.post(f"/api/trades/{ticket}/close").status_code == 200
    ev = walk(service, broker, [2652.30])
    assert ev[-1].kind == "closed"
    hist = client.get("/api/history").json()
    assert hist[0]["ticket"] == ticket and hist[0]["open"] is False
    events = client.get("/api/events").json()
    assert [e["kind"] for e in events][:2] == ["opened", "sl_moved"]

    # Re-enter after the close opens a fresh trade with the same settings.
    r = client.post(f"/api/trades/{ticket}/reenter")
    assert r.status_code == 201 and r.json()["ticket"] != ticket


def test_validation_errors_are_400(env):
    client, *_ = env
    r = client.post("/api/trades", json={"side": "sell", "settings": {"lot": 0.04, "tp_pips": 30}})
    assert r.status_code == 400 and "TP" in r.json()["detail"]


def test_limits_persist(env, tmp_path):
    client, service, broker = env
    r = client.put("/api/limits", json={"max_lot": 0.02, "daily_loss_usd": 20,
                                       "max_spread_pips": 8})
    assert r.status_code == 200 and r.json()["max_lot"] == 0.02
    r = client.post("/api/trades", json={"side": "buy", "settings": {"lot": 0.04}})
    assert r.status_code == 400 and "max lot" in r.json()["detail"]
    again = TradingService(broker, SqliteStore(tmp_path / "t.db"), sim_walk=False)
    assert again.engine.limits.max_lot == 0.02


def test_preset_crud(env):
    client, *_ = env
    r = client.post("/api/presets", json={"name": "Mine", "settings": {"lot": 0.02, "stop_mode": "tight"}})
    assert r.status_code == 201
    pid = r.json()["id"]
    r = client.put(f"/api/presets/{pid}", json={"name": "Mine 2", "settings": {"lot": 0.03}})
    assert r.json()["name"] == "Mine 2" and r.json()["settings"]["lot"] == 0.03
    assert client.delete(f"/api/presets/{pid}").status_code == 200
    assert client.delete(f"/api/presets/{pid}").status_code == 404


def test_sqlite_store_reloads_trades(tmp_path):
    store = SqliteStore(tmp_path / "s.db")
    service = TradingService(SimBroker(), store, sim_walk=False)
    service.open_trade(Side.BUY, TradeSettings())
    reloaded = SqliteStore(tmp_path / "s.db")
    assert len(reloaded.open_trades()) == 1
    assert len(reloaded.recent_events()) == 1


def test_adopt_untracked_position(env):
    client, service, broker = env
    # A position the bot doesn't know about (e.g. opened while it was down).
    r = broker.open(Side.BUY, 0.04, 2645.30, None, "manual")
    st = client.get("/api/status").json()
    assert [p["ticket"] for p in st["unmanaged"]] == [r.ticket]
    a = client.post(f"/api/trades/{r.ticket}/adopt", json={"lot": 0.04})
    assert a.status_code == 201 and a.json()["entry"] == 2650.30 and a.json()["sl"] == 2645.30
    assert client.get("/api/status").json()["unmanaged"] == []
    assert client.post(f"/api/trades/{r.ticket}/adopt", json={"lot": 0.04}).status_code == 400
    ev = walk(service, broker, [2652.30])          # steps run from now on
    assert ev[0].kind == "step_hit" and broker.position(r.ticket).volume == 0.02
