import pytest

from engine import Engine, EngineConfig, JsonStore, MemoryStore, RiskLimits, Side, StepConfig, StepStatus, \
    StopMode, TradeError, TradeSettings
from engine.broker import SymbolInfo
from engine.sim_broker import SimBroker
from engine.units import dollars, pips_to_price, split_volumes
from engine.validation import validate


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make(bid=2650.00, **kw):
    broker = SimBroker(bid=bid)
    clock = Clock()
    eng = Engine(broker, kw.pop("store", MemoryStore()), clock=clock, **kw)
    return eng, broker, clock


def walk(eng, broker, bids):
    events = []
    for b in bids:
        broker.set_price(b)
        events += eng.tick()
    return events


# ------------------------------------------------------------------- units

def test_pip_conversion():
    assert pips_to_price(20, 0.01) == 2.0
    assert pips_to_price(55, 0.01) == 5.5
    assert pips_to_price(33, 0.05) == 3.3
    assert pips_to_price(37, 0.05) == 3.7
    assert dollars(20, 1.0) == 200
    assert dollars(50, 0.10) == 50
    assert dollars(100, 0.01) == 10


def test_split_volumes_rounds_down_and_skips_below_min():
    assert split_volumes(0.04, [50, 25, 0], 0.01, 0.01) == [0.02, 0.01, 0.0]
    assert split_volumes(0.04, [50, 25, 25], 0.01, 0.01) == [0.02, 0.01, 0.01]
    assert split_volumes(0.03, [50, 25, 25], 0.01, 0.01) == [0.01, 0.0, 0.0]
    assert split_volumes(0.01, [50, 50, 0], 0.01, 0.01) == [0.0, 0.0, 0.0]


# -------------------------------------------------------------- validation

INFO = SymbolInfo(0.01, 2, 0.01, 0.01, 100, 0.0, 0.0)


def test_default_preset_is_valid():
    assert validate(TradeSettings(), INFO) == ([], [])


@pytest.mark.parametrize("settings, fragment", [
    (TradeSettings(steps=[StepConfig(50), StepConfig(20), StepConfig(100)]), "P2"),
    (TradeSettings(tp_pips=80), "TP"),
    (TradeSettings(steps=[StepConfig(20, 60), StepConfig(50, 30), StepConfig(100, 20)]), "adds up"),
    (TradeSettings(lot=0.015), "multiple"),
    (TradeSettings(steps=[StepConfig(20, 0, 30), StepConfig(50), StepConfig(100)]), "too close"),
])
def test_validation_errors(settings, fragment):
    errors, _ = validate(settings, INFO)
    assert any(fragment in e for e in errors), errors


def test_min_stop_distance():
    info = SymbolInfo(0.01, 2, 0.01, 0.01, 100, stops_level=3.0, freeze_level=0.0)  # 30 pips
    errors, _ = validate(TradeSettings(stop_mode=StopMode.TIGHT), info)
    assert any("broker minimum" in e for e in errors)


def test_rounding_warning():
    _, warnings = validate(TradeSettings(lot=0.03, steps=[StepConfig(20, 50), StepConfig(50, 25), StepConfig(100)]), INFO)
    assert any("P1 closes 0.01" in w for w in warnings)
    assert any("P2 close of 25" in w for w in warnings)


# ------------------------------------------------------- spec example (hybrid)

def test_spec_hybrid_example_buy():
    eng, broker, _ = make()
    trade, ev = eng.open_trade(Side.BUY, TradeSettings())
    assert trade.entry == 2650.30
    assert trade.sl == 2645.30                      # Medium = 50 pips
    assert ev[0].message == "Buy 0.04 @ 2650.30, SL 2645.30 (Medium)"

    ev = walk(eng, broker, [2651.00, 2652.29])
    assert ev == []                                 # +19.9 pips: nothing yet

    ev = walk(eng, broker, [2652.30])               # +20 on the bid
    assert ev[0].message == "P1 hit: closed 0.02 at +20 pips (+$4), SL to breakeven"
    assert broker.position(trade.ticket).volume == 0.02
    assert broker.position(trade.ticket).sl == 2650.40   # entry + 1 pip buffer

    ev = walk(eng, broker, [2655.30])
    assert ev[0].message == "P2 hit: closed 0.01 at +50 pips (+$5), SL to +20"
    assert broker.position(trade.ticket).sl == 2652.30

    ev = walk(eng, broker, [2660.30])
    assert ev[0].message == "P3 hit: SL to +50"
    pos = broker.position(trade.ticket)
    assert pos.volume == 0.01 and pos.sl == 2655.30

    ev = walk(eng, broker, [2665.30])               # +150: trailing moves SL by 50 more
    assert ev[0].message == "Trailing: SL moved to 2660.30 (+100 pips)"

    ev = walk(eng, broker, [2662.00, 2660.30])      # reverses into the stop (server side)
    assert ev[-1].kind == "closed"
    assert ev[-1].message == "Closed by SL at +100 pips. Total +$19"   # 4 + 5 + 10
    assert ev[-1].action == "Re-enter"
    assert not eng.store.trades[trade.ticket].open


def test_sell_mirrors_buy():
    eng, broker, _ = make()
    trade, _ = eng.open_trade(Side.SELL, TradeSettings(stop_mode=StopMode.TIGHT))
    assert trade.entry == 2650.00 and trade.sl == 2652.00
    ev = walk(eng, broker, [2647.70])               # ask = 2648.00 = +20 pips
    assert "P1 hit: closed 0.02 at +20 pips" in ev[0].message
    assert broker.position(trade.ticket).sl == 2649.90


def test_single_jump_past_everything():
    eng, broker, _ = make()
    trade, _ = eng.open_trade(Side.BUY, TradeSettings(trailing=False))
    ev = walk(eng, broker, [2662.00])
    assert [e.message.split(":")[0] for e in ev] == ["P1 hit", "P2 hit", "P3 hit"]
    pos = broker.position(trade.ticket)
    assert pos.volume == 0.01 and pos.sl == 2655.30
    # Partial closes happened at the gapped price, and P/L reflects that.
    assert "+117 pips" in ev[0].message


def test_partial_close_only_closes_everything_at_p3():
    eng, broker, _ = make()
    s = TradeSettings(steps=[StepConfig(20, 50), StepConfig(50, 25), StepConfig(100, 25)])
    trade, _ = eng.open_trade(Side.BUY, s)
    ev = walk(eng, broker, [2652.30, 2655.30, 2660.30, 2660.30])
    assert ev[-1].kind == "closed"
    assert ev[-1].message.startswith("Closed by profit steps")
    assert eng.store.trades[trade.ticket].result_usd == 4 + 5 + 10


def test_move_stop_only_keeps_full_volume():
    eng, broker, _ = make()
    s = TradeSettings(steps=[StepConfig(20, 0, 0), StepConfig(50, 0, 20), StepConfig(100, 0, 50)])
    trade, _ = eng.open_trade(Side.BUY, s)
    walk(eng, broker, [2652.30, 2655.30])
    pos = broker.position(trade.ticket)
    assert pos.volume == 0.04 and pos.sl == 2652.30


# ------------------------------------------------------------ retries/errors

def test_retry_then_success():
    eng, broker, clock = make()
    trade, _ = eng.open_trade(Side.BUY, TradeSettings())
    broker.fail("close", times=2)
    assert walk(eng, broker, [2652.30]) == []
    assert trade.steps[0].attempts == 1
    assert walk(eng, broker, [2652.30]) == []      # still backing off: no new attempt
    assert trade.steps[0].attempts == 1
    clock.t += 2
    walk(eng, broker, [2652.30])
    assert trade.steps[0].attempts == 2
    clock.t += 4
    ev = walk(eng, broker, [2652.30])
    assert ev[0].kind == "step_hit"


def test_three_failures_mark_step_failed_and_alert():
    eng, broker, clock = make()
    trade, _ = eng.open_trade(Side.BUY, TradeSettings())
    broker.fail("close", 10018, "market closed", times=3)
    ev = []
    for _ in range(3):
        ev += walk(eng, broker, [2652.30])
        clock.t += 10
    assert trade.steps[0].status is StepStatus.FAILED
    assert ev[-1].message == "Partial close at P1 rejected: market closed"
    assert ev[-1].action == "Retry"
    # Later steps still run.
    ev = walk(eng, broker, [2655.30])
    assert ev[0].message.startswith("P2 hit")
    eng.retry_step(trade.ticket, 0)
    assert trade.steps[0].status is StepStatus.PENDING


def test_sl_move_failure_does_not_repeat_close():
    eng, broker, clock = make()
    trade, _ = eng.open_trade(Side.BUY, TradeSettings())
    broker.fail("modify")
    walk(eng, broker, [2652.30])
    assert trade.steps[0].close_done and not trade.steps[0].sl_done
    clock.t += 5
    ev = walk(eng, broker, [2652.30])
    assert ev[0].kind == "step_hit"
    assert [c[0] for c in broker.calls].count("close") == 1
    assert broker.position(trade.ticket).volume == 0.02


# ------------------------------------------------------------------ restart

def test_restart_recovers_without_refiring(tmp_path):
    path = tmp_path / "state.json"
    broker = SimBroker()
    eng = Engine(broker, JsonStore(path))
    trade, _ = eng.open_trade(Side.BUY, TradeSettings())
    broker.set_price(2652.30)
    eng.tick()

    # Bot goes down; price keeps moving.
    eng2 = Engine(broker, JsonStore(path))
    t2 = eng2.store.trades[trade.ticket]
    assert t2.steps[0].status is StepStatus.HIT
    ev = walk(eng2, broker, [2652.50, 2655.30])
    assert [e.message.split(":")[0] for e in ev] == ["P2 hit"]
    assert [c[0] for c in broker.calls].count("close") == 2


def test_sl_hit_while_bot_down_is_reported_on_restart(tmp_path):
    path = tmp_path / "state.json"
    broker = SimBroker()
    eng = Engine(broker, JsonStore(path))
    trade, _ = eng.open_trade(Side.BUY, TradeSettings())
    broker.set_price(2645.00)                       # server SL fires, bot not running
    ev = Engine(broker, JsonStore(path)).tick()
    assert ev[0].message == "Closed by SL at -50 pips. Total -$20"


# --------------------------------------------------------------- risk/modes

def test_risk_limits_block_new_trades():
    eng, broker, _ = make(limits=RiskLimits(max_lot=0.05, max_open_trades=1, max_spread_pips=10))
    with pytest.raises(TradeError, match="max lot"):
        eng.open_trade(Side.BUY, TradeSettings(lot=0.06))
    broker.spread = 1.50
    with pytest.raises(TradeError, match="Spread is 15 pips"):
        eng.open_trade(Side.BUY, TradeSettings())
    broker.spread = 0.30
    eng.open_trade(Side.BUY, TradeSettings())
    with pytest.raises(TradeError, match="trades open"):
        eng.open_trade(Side.BUY, TradeSettings())


def test_market_closed_blocks_trades():
    eng, broker, _ = make()
    broker.is_open = False
    with pytest.raises(TradeError, match="Market closed"):
        eng.open_trade(Side.BUY, TradeSettings())


def test_daily_loss_limit(monkeypatch):
    import time as _t
    broker = SimBroker()
    eng = Engine(broker, MemoryStore(), limits=RiskLimits(daily_loss_usd=15), clock=_t.time)
    eng.open_trade(Side.BUY, TradeSettings())
    walk(eng, broker, [2645.00])                    # -$20
    with pytest.raises(TradeError, match="Daily loss limit"):
        eng.open_trade(Side.BUY, TradeSettings())


def test_far_mode_uses_backup_sl_until_set():
    eng, broker, _ = make()
    trade, ev = eng.open_trade(Side.BUY, TradeSettings(stop_mode=StopMode.FAR))
    assert trade.sl == 2620.30                       # 300 pip backup
    assert "(backup)" in ev[0].message
    ev = eng.modify_trade(trade.ticket, sl_pips=-80)
    assert ev[0].message == "SL moved to 2642.30"
    assert broker.position(trade.ticket).sl == 2642.30


def test_edit_pending_step_and_tp():
    eng, broker, _ = make()
    trade, _ = eng.open_trade(Side.BUY, TradeSettings())
    eng.modify_trade(trade.ticket, tp_pips=200, steps={0: {"pips": 30, "close_pct": 25}})
    assert broker.position(trade.ticket).tp == 2670.30
    assert trade.steps[0].planned_volume == 0.01
    assert walk(eng, broker, [2652.30]) == []       # old P1 no longer fires
    walk(eng, broker, [2653.30])
    with pytest.raises(TradeError, match="already hit"):
        eng.modify_trade(trade.ticket, steps={0: {"pips": 25}})


def test_manual_close_and_reenter():
    eng, broker, _ = make()
    trade, _ = eng.open_trade(Side.SELL, TradeSettings())
    with pytest.raises(TradeError, match="full close"):
        eng.reenter(trade.ticket)
    eng.close_trade(trade.ticket)
    ev = eng.tick()
    assert ev[0].kind == "closed"
    new, ev = eng.reenter(trade.ticket)
    assert new.ticket != trade.ticket and new.side is Side.SELL and new.settings == trade.settings


def test_preview_shows_prices_and_dollars():
    eng, _, _ = make()
    p = eng.preview(Side.BUY, TradeSettings())
    assert p["sl"] == {"price": 2645.30, "usd": -20.0}
    assert p["steps"][0] == {"price": 2652.30, "close_volume": 0.02, "usd": 4.0}
