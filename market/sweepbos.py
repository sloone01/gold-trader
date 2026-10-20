"""Sweep+BOS strategy (SMC-style), beside the main signal.

  1. 4H structure direction (EMA proxy for HH/HL vs LH/LL); unclear = no trade
  2. 1H swing support/resistance zones (clustered swing points, 2+ touches)
  3. 5M liquidity sweep: price pierces a zone and closes back inside
  4. 5M break of structure: a close beyond the last minor swing
  5. Stop behind the sweep extreme, target the opposite zone (at least MIN_RR)

Gold setups go to Signals as the "Sweep+BOS" channel, so the gold engine manages them like
any channel (weight, mode, P1-P3). Other assets in SWEEP_BOS_ASSETS are ordered through MT5
with their own magic number.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .config import ASSETS, TIMEFRAMES, _bool, _list, _num
from .indicators import ema_series, sma_series

log = logging.getLogger("gold-trader.market")
FEED = "sweep-bos"


@dataclass
class SweepBosConfig:
    enabled: bool = False
    assets: list[str] = field(default_factory=lambda: ["gold"])
    magic: int = 260732
    risk_percent: float = 0.5
    min_rr: float = 2
    zone_band_pct: float = 0.12
    minor_lookback: int = 8
    bos_within: int = 24
    cooldown_bars: int = 24

    @staticmethod
    def from_env() -> "SweepBosConfig":
        return SweepBosConfig(
            enabled=_bool("SWEEP_BOS_ENABLED", False), assets=_list("SWEEP_BOS_ASSETS", "gold"),
            magic=int(_num("SWEEP_BOS_MAGIC", 260732)), risk_percent=_num("SWEEP_BOS_RISK_PERCENT", 0.5),
            min_rr=_num("SWEEP_BOS_MIN_RR", 2), zone_band_pct=_num("SWEEP_BOS_ZONE_BAND_PCT", 0.12),
            minor_lookback=int(_num("SWEEP_BOS_MINOR_LOOKBACK", 8)), bos_within=int(_num("SWEEP_BOS_WITHIN", 24)),
            cooldown_bars=int(_num("SWEEP_BOS_COOLDOWN_BARS", 24)))


# ------------------------------------------------------- pure detection

def structure_bias(h1: list[dict], upto: int | None = None) -> int:
    """4H bias from 1H candles: +1 up, -1 down, 0 unclear."""
    upto = len(h1) if upto is None else upto
    closes = [h1[i]["close"] for i in range(3, upto, 4)]
    if len(closes) < 55:
        return 0
    e20, e50, s50 = ema_series(closes, 20), ema_series(closes, 50), sma_series(closes, 50)
    if s50[-1] is None:
        return 0
    if e20[-1] > e50[-1] and closes[-1] > s50[-1]:
        return 1
    if e20[-1] < e50[-1] and closes[-1] < s50[-1]:
        return -1
    return 0


def find_zones(h1: list[dict], band_pct: float, upto: int | None = None) -> list[dict]:
    """Clustered swing S/R zones with 2+ touches: {level, low, high, touches}."""
    upto = len(h1) if upto is None else upto
    swings = []
    for i in range(2, upto - 2):
        c, n = h1[i], [h1[i - 2], h1[i - 1], h1[i + 1], h1[i + 2]]
        if all(c["low"] < x["low"] for x in n):
            swings.append(c["low"])
        if all(c["high"] > x["high"] for x in n):
            swings.append(c["high"])
    zones: list[dict] = []
    for level in sorted(swings):
        z = next((z for z in zones if abs(z["level"] - level) <= level * band_pct / 100), None)
        if z:
            z["touches"] += 1
            z["level"] = (z["level"] * (z["touches"] - 1) + level) / z["touches"]
        else:
            zones.append({"level": level, "touches": 1})
    for z in zones:
        band = z["level"] * band_pct / 100
        z["low"], z["high"] = z["level"] - band, z["level"] + band
    return [z for z in zones if z["touches"] >= 2]


def detect_setups(bars: list[dict], zones: list[dict], bias: int, cfg: SweepBosConfig, spread: float = 0) -> list[dict]:
    """Sweep -> BOS events on 5M bars: {idx, side, sl, risk, target, rr}. idx = the bar whose close confirmed the BOS."""
    events, sweep, cooldown = [], None, -1
    for i in range(2, len(bars)):
        bar = bars[i]
        if sweep and (i > sweep["expires"] or (sweep["side"] == "buy" and bar["close"] < sweep["extreme"])
                      or (sweep["side"] == "sell" and bar["close"] > sweep["extreme"])):
            sweep = None
        if sweep and i > cooldown:
            bos = bar["close"] > sweep["minor"] if sweep["side"] == "buy" else bar["close"] < sweep["minor"]
            if bos:
                ref = bar["close"]
                margin = max(ref * 0.0005, spread * 2)
                sl = sweep["extreme"] - margin if sweep["side"] == "buy" else sweep["extreme"] + margin
                risk = abs(ref - sl)
                if sweep["side"] == "buy":
                    above = sorted((z for z in zones if z["low"] > ref), key=lambda z: z["low"])
                    target = above[0]["low"] if above else None
                else:
                    below = sorted((z for z in zones if z["high"] < ref), key=lambda z: -z["high"])
                    target = below[0]["high"] if below else None
                rr = abs(target - ref) / risk if target is not None and risk > 0 else 0
                if risk > 0 and target is not None and rr >= cfg.min_rr:
                    events.append({"idx": i, "side": sweep["side"], "sl": sl, "risk": risk, "target": target, "rr": rr})
                    cooldown = i + cfg.cooldown_bars
                sweep = None
                continue
        if not sweep and i > cooldown and bias != 0:
            look = bars[max(0, i - cfg.minor_lookback):i]
            if bias > 0:
                z = next((z for z in zones if bar["low"] < z["low"] and bar["close"] > z["low"]), None)
                if z:
                    sweep = {"side": "buy", "zone": z, "extreme": bar["low"], "minor": max(b["high"] for b in look),
                             "expires": i + cfg.bos_within}
            else:
                z = next((z for z in zones if bar["high"] > z["high"] and bar["close"] < z["high"]), None)
                if z:
                    sweep = {"side": "sell", "zone": z, "extreme": bar["high"], "minor": min(b["low"] for b in look),
                             "expires": i + cfg.bos_within}
        elif sweep:     # a deeper wick while waiting moves the sweep extreme
            if sweep["side"] == "buy" and bar["low"] < sweep["extreme"] and bar["close"] > sweep["zone"]["low"]:
                sweep["extreme"] = bar["low"]
            elif sweep["side"] == "sell" and bar["high"] > sweep["extreme"] and bar["close"] < sweep["zone"]["high"]:
                sweep["extreme"] = bar["high"]
    return events


# ---------------------------------------------------------------- live

class SweepBos:
    def __init__(self, cfg: SweepBosConfig, market, signals=None, mt=None, symbols: dict | None = None) -> None:
        self.cfg = cfg
        self.market = market        # MarketService: candles (broker for gold) + news blackout config
        self.signals = signals      # SignalService: gold setups become the Sweep+BOS channel
        self.mt = mt                # MT5Multi for non-gold assets (None = gold only)
        self.symbols = symbols or {}
        self.watching: dict[str, str] = {}
        self.actions: list[dict] = []
        self.last_run: float | None = None
        self.error: str | None = None
        self._fired: dict[str, str] = {}

    def status(self) -> dict:
        return {"enabled": self.cfg.enabled, "assets": self.cfg.assets, "watching": self.watching,
                "last_run": self.last_run, "error": self.error, "actions": self.actions[:15]}

    def run(self) -> None:
        if not self.cfg.enabled:
            return
        if getattr(getattr(self.market, "trading", None), "automation_paused", False):
            self.watching = {a: "automatic trading paused" for a in self.cfg.assets}
            return
        self.last_run, self.error = time.time(), None
        from . import providers as pv
        positions = []
        try:
            positions = self.mt.positions(self.cfg.magic) if self.mt else []
        except Exception as ex:
            self.error = str(ex)
        for aid in self.cfg.assets:
            asset = next((a for a in ASSETS if a["id"] == aid), None)
            if asset is None:
                continue
            try:
                self.watching[aid] = self._scan(asset, positions, pv)
            except Exception as ex:
                self.watching[aid] = f"error: {ex}"
                log.warning("sweep+bos %s: %s", aid, ex)

    def _scan(self, asset: dict, positions: list[dict], pv) -> str:
        aid = asset["id"]
        symbol = self.symbols.get(aid)
        if aid != "gold" and (self.mt is None or not symbol):
            return "no MT5 symbol (gold only without MT5)"
        if aid != "gold" and any(p["symbol"] == symbol for p in positions):
            return "position open"
        h1, h1_demo = self.market.fetch(asset, TIMEFRAMES["1h"])
        m5, m5_demo = self.market.fetch(asset, TIMEFRAMES["5m"])
        if h1_demo or m5_demo:
            return "no live prices (demo data)"
        bias = structure_bias(h1["candles"])
        if bias == 0:
            return "4h structure unclear, standing aside"
        zones = find_zones(h1["candles"], self.cfg.zone_band_pct)
        if not zones:
            return "no qualified 1h zones"
        bars = m5["candles"]
        events = detect_setups(bars, zones, bias, self.cfg)
        last = events[-1] if events else None
        d = "up" if bias > 0 else "down"
        if not last:
            return f"bias {d}, {len(zones)} zones, waiting for a 5m sweep + BOS"
        if last["idx"] != len(bars) - 1:
            return f"bias {d}, last {last['side']} {len(bars) - 1 - last['idx']} bars ago, waiting for a fresh BOS"
        bar_time = str(bars[-1]["time"])
        if self._fired.get(aid) == bar_time:
            return f"bias {d}, already traded this BOS"
        ev = pv.news_blackout(aid, self.market.cfg)
        if ev:
            return f"news blackout: {ev['currency']} {ev['title']}"
        self._fired[aid] = bar_time
        self._enter(asset, symbol, last, m5)
        return f"bias {d}, BOS confirmed: {last['side']} sent"

    def _enter(self, asset: dict, symbol: str | None, ev: dict, m5: dict) -> None:
        side, price = ev["side"], m5["price"]
        if asset["id"] == "gold":
            if self.signals is None:
                return
            sign = 1 if side == "buy" else -1
            tps = [round(price + sign * ev["risk"] * 1, 2), round(price + sign * ev["risk"] * 2, 2), round(ev["target"], 2)]
            sig = self.signals.ingest(FEED, f"{side}-{m5['candles'][-1]['time']}", "open", side=side, entry=price,
                                      sl=round(ev["sl"], 2), tps=sorted(tps, reverse=side == "sell"), title="Sweep+BOS",
                                      note=f"Sweep+BOS {side.upper()}: sweep of a 1h zone + 5m break of structure, "
                                           f"R:R {ev['rr']:.1f}", reanchor=m5.get("source") != "mt5")
            self._log(asset["id"], side, sig.status if sig else "channel_off", ev)
            return
        res = self.mt.order(symbol, side, ev["sl"], ev["target"], entry=price, risk_percent=self.cfg.risk_percent,
                            tp_levels=[{"part": 50, "r": 1}, {"part": 30, "r": 2}, {"part": 20, "r": 0}],
                            magic=self.cfg.magic, comment=f"SB-{asset['id']}-{side}"[:31])
        self._log(asset["id"], side, f"ordered {res.get('volume')}", ev)

    def _log(self, aid: str, side: str, status: str, ev: dict) -> None:
        self.actions.insert(0, {"asset": aid, "side": side, "status": status, "sl": ev["sl"], "target": ev["target"],
                                "rr": round(ev["rr"], 2), "at": time.time()})
        del self.actions[40:]
        log.info("sweep+bos %s %s: %s (rr %.2f)", aid, side, status, ev["rr"])
