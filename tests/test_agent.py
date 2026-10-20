"""The signal agent in shadow mode, with a fake Claude client (no network)."""
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from engine import SqliteStore
from engine.sim_broker import SimBroker
from server import TradingService, create_app
from signals import Channel, ParsedSignal, SignalService
from signals.agent import TOOLS, Knowledge, SignalAgent, describe

TOKEN = "t"
CH = -1001


class FakeClaude:
    """Answers with scripted tool calls and records every request."""

    def __init__(self):
        self.requests, self.script = [], []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        calls = self.script.pop(0) if self.script else [("no_action", {"kind": "chat", "reason": "x"})]
        content = [NS(type="tool_use", id=f"t{i}", name=n, input=inp) for i, (n, inp) in enumerate(calls)]
        return NS(content=content, stop_reason="tool_use", model=kw["model"],
                  usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=0))

    def system_text(self, i=-1):
        return "\n".join(b["text"] for b in self.requests[i]["system"])

    def user_text(self, i=-1):
        return self.requests[i]["messages"][0]["content"][-1]["text"]


class Parser:
    def __init__(self):
        self.map = {}

    def __call__(self, text, context="", **kw):
        return self.map.get(text, ParsedSignal(action="ignore", reason="chat"))


@pytest.fixture
def env(tmp_path):
    trading = TradingService(SimBroker(bid=2650.0), SqliteStore(tmp_path / "a.db"), sim_walk=False)
    parser, claude = Parser(), FakeClaude()
    sigs = SignalService(trading, parser)
    sigs.agent = SignalAgent(Knowledge(trading.store), client=claude)
    sigs.agent_async = False
    sigs.cfg.agent_mode = "shadow"
    sigs.save_channel(Channel(CH, "Majeed Forex", weight=3, mode="auto"))
    app = create_app(trading, token=TOKEN, run_loop=False, signals=sigs)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        yield client, trading, sigs, parser.map, claude


def test_tools_are_strict_and_complete():
    for t in TOOLS:
        s = t["input_schema"]
        assert t["strict"] and s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])


def test_shadow_sees_state_and_records_without_trading(env):
    client, trading, sigs, m, claude = env
    m["BUY gold 2650 SL 2640 TP 2660"] = ParsedSignal(action="open", symbol="XAUUSD", side="buy", entry=2650,
                                                     sl=2640, tps=[2660], confidence=0.9)
    claude.script = [[("open_trade", {"side": "buy", "order_type": "market", "entry": 2650, "entry_high": None,
                                      "sl": 2640, "tps": [2660], "reason": "clear buy"})],
                     [("close", {"targets": ["T1"], "percent": 100, "reason": "close the old one"})]]
    first = sigs.handle_message(CH, "Majeed Forex", 1, "BUY gold 2650 SL 2640 TP 2660")
    assert first.status == "placed"                                   # the parser path still trades
    r = sigs.agent_result(first.id)
    assert r["actions"][0]["tool"] == "open_trade" and r["mode"] == "shadow"
    assert "Open trades from this channel: none" in claude.user_text()
    second = sigs.handle_message(CH, "Majeed Forex", 2, "close the old one keep the new")
    assert "T1: buy" in claude.user_text() and "BUY gold 2650" in claude.user_text()   # state + conversation
    r2 = sigs.agent_result(second.id)
    assert r2["inputs"]["refs"]["T1"]["ticket"] == first.ticket
    assert describe(r2["actions"][0]) == "Close T1"
    assert trading.store.trades[first.ticket].open                     # shadow: nothing closed
    listed = client.get("/api/signals").json()
    assert listed[-1]["agent"]["actions"][0]["tool"] == "close" and "inputs" not in listed[-1]["agent"]


def test_teach_saves_lesson_and_rereads(env):
    client, trading, sigs, m, claude = env
    sig = sigs.handle_message(CH, "Majeed Forex", 1, "gold 🔥 now 50")            # parser ignores it
    assert sig.status == "ignored"
    claude.script = [[("open_trade", {"side": "buy", "order_type": "market", "entry": None, "entry_high": None,
                                      "sl": None, "tps": [], "reason": "taught"})]]
    r = client.post(f"/api/signals/{sig.id}/teach", json={"text": "'gold 🔥 now' from this channel means buy at market", "scope": "channel"})
    assert r.status_code == 200 and r.json()["relearned"] and r.json()["actions"][0]["tool"] == "open_trade"
    assert "means buy at market (from you)" in claude.system_text()                  # the lesson is in the prompt
    k = client.get("/api/agent").json()["knowledge"]
    assert k["channels"][str(CH)][0]["source"] == "you" and k["channels"][str(CH)][0]["example"] == "gold 🔥 now 50"
    client.post(f"/api/signals/{sig.id}/teach", json={"text": "Results like '+40' are reports", "scope": "shared"})
    assert client.get("/api/agent").json()["knowledge"]["shared"][0]["text"] == "Results like '+40' are reports"


def test_agent_questions_and_answers(env):
    client, trading, sigs, m, claude = env
    claude.script = [[("ask_me", {"question": "Does 'secure' mean close half or move to BE?", "reason": "unclear"})],
                     [("move_sl", {"targets": ["T1"], "breakeven": True, "price": None, "reason": "you said BE"})]]
    sig = sigs.handle_message(CH, "Majeed Forex", 5, "secure")
    qs = client.get("/api/agent").json()["questions"]
    assert qs[0]["signal_id"] == sig.id and "secure" in qs[0]["question"]
    assert any("Agent asks" in e["message"] for e in trading.store.recent_events(10))
    r = client.post(f"/api/signals/{sig.id}/answer", json={"text": "move the stop to breakeven"}).json()
    assert r["actions"][0]["tool"] == "move_sl" and r["answered"]["answer"] == "move the stop to breakeven"
    assert client.get("/api/agent").json()["questions"] == []
    note = client.get("/api/agent").json()["knowledge"]["channels"][str(CH)][0]
    assert note["text"].startswith("Q: Does 'secure'") and "A: move the stop to breakeven" in note["text"]


def test_agent_notes_crud_and_own_notes(env):
    client, trading, sigs, m, claude = env
    claude.script = [[("no_action", {"kind": "result", "reason": "report"}),
                      ("note_for_playbook", {"scope": "channel", "text": "Posts '+N' after a TP", "reason": "habit"})]]
    sig = sigs.handle_message(CH, "Majeed Forex", 1, "+40")
    assert sigs.agent_result(sig.id)["notes_saved"][0]["text"] == "Posts '+N' after a TP"
    n = client.post("/api/agent/notes", json={"text": "Always 3 TPs", "scope": "channel", "channel_id": CH}).json()
    assert client.patch(f"/api/agent/notes/{n['id']}", json={"text": "Usually 3 TPs"}).status_code == 200
    assert client.delete(f"/api/agent/notes/{n['id']}").status_code == 200
    texts = [x["text"] for x in client.get("/api/agent").json()["knowledge"]["channels"][str(CH)]]
    assert texts == ["Posts '+N' after a TP"]


def test_agent_off_makes_no_calls(env):
    client, trading, sigs, m, claude = env
    sigs.cfg.agent_mode = "off"
    sigs.handle_message(CH, "Majeed Forex", 1, "hello")
    assert claude.requests == []
