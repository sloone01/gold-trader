import time

import pytest

from market.autotrader import AutoTradeConfig, AutoTrader, summarize_deals
from market.sweepbos import SweepBos, SweepBosConfig, detect_setups, find_zones


class FakeMT:
    def __init__(self, demo=True):
        self.demo, self.pos, self.orders, self.mods, self.closed, self.n = demo, [], [], [], [], 100

    def account(self):
        return {"login": 1, "server": "x", "is_demo": self.demo, "balance": 1e4, "equity": 1e4}

    def positions(self, magic=None):
        return [p for p in self.pos if magic is None or p["magic"] == magic]

    def order(self, symbol, side, stop, take_profit=None, **kw):
        self.n += 1
        self.orders.append({"symbol": symbol, "side": side, "stop": stop, "tp": take_profit, **kw})
        self.pos.append({"ticket": self.n, "symbol": symbol, "type": side, "volume": 0.1, "price_open": 100.0,
                         "price_current": 100.0, "sl": stop, "tp": take_profit or 0, "time": time.time(), "magic": kw["magic"]})
        return {"volume": 0.1, "sl": stop, "tp": take_profit}

    def modify(self, ticket, sl, tp=None):
        self.mods.append((ticket, round(sl, 4)))
        next(p for p in self.pos if p["ticket"] == ticket)["sl"] = sl
        return {"sl": sl}

    def close(self, ticket):
        self.closed.append(ticket)
        self.pos = [p for p in self.pos if p["ticket"] != ticket]
        return {"ok": True}

    def deals(self, days, magic=None):
        return []

    def candles(self, symbol, tf, count):
        return {"candles": [{"time": i, "open": 100, "high": 101, "low": 99, "close": 100 + i * 0.01} for i in range(count)],
                "price": 102}


def asset(aid, side, conf=60, score=40, active=True, price=100.0):
    stop = price - 1 if side == "buy" else price + 1
    target = price + 2 if side == "buy" else price - 2
    return {"id": aid, "price": price, "demo_price": False,
            "signal": {"confidence": conf, "composite": score if side == "buy" else -score,
                       "trade": {"active": active, "side": side, "entry": price, "stop": stop, "target": target}}}


def snap(*assets, tf="4h"):
    return {"timeframe": {"id": tf}, "assets": list(assets)}


def make(mode="mt5", demo=True, **kw):
    cfg = AutoTradeConfig(**{"enabled": True, "mode": mode, "block_hours": [], "confirm_trend": False, "feedback": False, **kw})
    mt = FakeMT(demo)
    return AutoTrader(cfg, market=None, mt=mt), mt


def test_gold_is_never_auto_traded_and_dry_run_logs_only():
    at, mt = make(mode="dry_run")
    at.run(snap(asset("gold", "buy"), asset("silver", "sell")))
    assert [a["symbol"] for a in at.actions] == ["XAGUSD"] and at.actions[0]["type"] == "would_open"
    assert mt.orders == [] and at.status()["mode"] == "dry_run"


def test_opens_sized_by_level_and_closes_on_flip():
    at, mt = make()
    at.run(snap(asset("silver", "buy", conf=60, score=45), asset("eurusd", "sell", conf=41, score=21)))
    by = {o["symbol"]: o for o in mt.orders}
    assert by["XAGUSD"]["risk_percent"] == 5 and by["EURUSD"]["risk_percent"] == 2    # level 3 vs level 1
    at.run(snap(asset("silver", "buy")))                                           # same idea: no second order
    assert len(mt.orders) == 2
    at.run(snap(asset("silver", "sell")))                                          # flip: close then reopen
    assert len(mt.closed) == 1 and mt.orders[-1]["side"] == "sell"


def test_live_account_blocked_and_wrong_timeframe_ignored():
    at, mt = make(demo=False)
    at.run(snap(asset("silver", "buy")))
    assert mt.orders == [] and at.status()["mode"] == "blocked_live" and "AUTO_TRADE_ALLOW_LIVE" in at.error
    at2, mt2 = make()
    at2.run(snap(asset("silver", "buy"), tf="1h"))
    assert mt2.orders == []


def test_break_even_then_trail():
    at, mt = make()
    at.run(snap(asset("silver", "buy")))
    p = mt.pos[0]
    p["price_current"] = 100.6                                # +0.6R -> break-even
    at.manage(mt.positions(), {at.cfg.magic})
    assert mt.mods[-1] == (p["ticket"], 100.05)
    p["price_current"] = 101.5                                # +1.5R -> trail 0.7R behind
    at.manage(mt.positions(), {at.cfg.magic})
    assert mt.mods[-1] == (p["ticket"], 100.8)


def test_news_blackout_and_trend_filter_skip():
    at, mt = make()
    at.news_blackout = lambda aid: {"currency": "USD", "title": "CPI"}
    at.run(snap(asset("silver", "buy")))
    assert mt.orders == [] and "news blackout" in at.actions[0]["reason"]
    at2, mt2 = make(confirm_trend=True)
    at2.run(snap(asset("oil", "sell")))                       # fake candles trend up -> sell skipped
    assert mt2.orders == [] and "against" in at2.actions[0]["reason"]


def test_summarize_deals():
    deals = [{"symbol": "XAGUSD", "position_id": 1, "type": 0, "entry": 0, "time": 0, "volume": 1, "price": 30,
              "profit": 0, "commission": -1, "swap": 0, "fee": 0, "comment": "MW"},
             {"symbol": "XAGUSD", "position_id": 1, "type": 1, "entry": 1, "time": 600, "volume": 1, "price": 31,
              "profit": 51, "commission": 0, "swap": 0, "fee": 0, "comment": "[tp 31]"}]
    r = summarize_deals(deals, 7)
    assert r["closed_trades"] == 1 and r["net_profit"] == 50 and r["by_exit_reason"]["take_profit"]["trades"] == 1


def test_sweep_bos_gold_setup_goes_to_its_channel(monkeypatch):
    # 1h: a clean uptrend with a twice-touched zone at ~100; 5m: sweep under the zone, then break of structure.
    h1 = []
    for i in range(300):
        base = 80 + i * 0.1
        h1.append({"time": i, "open": base, "high": base + 0.5, "low": base - 0.5, "close": base + 0.2})
    for i in (150, 170):                                       # a support zone touched twice...
        h1[i] = {**h1[i], "low": 94.0}
    for i in (260, 280):                                       # ...and a resistance zone above as the target
        h1[i] = {**h1[i], "high": 120.0}
    cfg = SweepBosConfig(enabled=True, min_rr=1, zone_band_pct=0.5)
    zones = find_zones(h1, cfg.zone_band_pct)
    assert any(z["touches"] >= 2 for z in zones)
    z = next(z for z in zones if z["touches"] >= 2)
    bars = [{"time": i, "open": z["level"] + 1, "high": z["level"] + 1.2, "low": z["level"] + 0.8, "close": z["level"] + 1}
            for i in range(20)]
    bars.append({"time": 20, "open": z["level"] + 1, "high": z["level"] + 1.1, "low": z["low"] - 0.5, "close": z["low"] + 0.1})
    bars.append({"time": 21, "open": z["low"] + 0.1, "high": z["level"] + 2, "low": z["low"], "close": z["level"] + 1.5})
    ev = detect_setups(bars, zones, 1, cfg)
    assert ev and ev[-1]["side"] == "buy" and ev[-1]["idx"] == len(bars) - 1

    sent = []

    class Sigs:
        def ingest(self, feed, key, action, **kw):
            sent.append((feed, action, kw))
            return type("S", (), {"status": "pending", "error": None})()

    class Mkt:
        cfg = type("C", (), {"news_filter": False})()

        def fetch(self, asset, tf):
            return ({"candles": h1, "price": h1[-1]["close"], "source": "mt5"} if tf["id"] == "1h"
                    else {"candles": bars, "price": bars[-1]["close"], "source": "mt5"}), False

    import market.sweepbos as sbm
    monkeypatch.setattr(sbm, "structure_bias", lambda h: 1)
    sb = SweepBos(cfg, Mkt(), Sigs())
    sb.run()
    assert sent and sent[0][0] == "sweep-bos" and sent[0][2]["side"] == "buy" and len(sent[0][2]["tps"]) == 3
    assert "BOS confirmed" in sb.watching["gold"]
    sb.run()                                                   # same bar: not sent twice
    assert len(sent) == 1 and "already traded" in sb.watching["gold"]
