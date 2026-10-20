"""Auto-trader for the non-gold Markets assets (silver, oil, forex, crypto).

Gold never goes through here: its idea is a Signals channel and the gold engine manages it.
For the rest, active buy/sell ideas become MT5 orders sized by risk %, with optional partial-TP
tickets, break-even and trailing stops, a time stop, higher-timeframe trend confirmation, a
news blackout, session block hours and a feedback loop that pauses symbols on a losing streak.

Off by default; dry_run logs what it would do; mt5 places orders (demo accounts only unless
AUTO_TRADE_ALLOW_LIVE=true). Settings keep the old Market Watch AUTO_TRADE_* names.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

from .config import ASSETS, TIMEFRAMES, _bool, _list, _num
from .indicators import compute

log = logging.getLogger("gold-trader.market")
DEFAULT_SYMBOLS = {"silver": "XAGUSD", "oil": "USOIL", "eurusd": "EURUSD", "gbpusd": "GBPUSD", "usdjpy": "USDJPY",
                   "bitcoin": "BTCUSD", "ethereum": "ETHUSD"}
SYMBOL_ENV = {"silver": "MT5_SYMBOL_SILVER", "oil": "MT5_SYMBOL_OIL", "eurusd": "MT5_SYMBOL_EURUSD",
              "gbpusd": "MT5_SYMBOL_GBPUSD", "usdjpy": "MT5_SYMBOL_USDJPY", "bitcoin": "MT5_SYMBOL_BTC",
              "ethereum": "MT5_SYMBOL_ETH"}


def _nums(name: str, default: str) -> list[float]:
    out = []
    for s in os.environ.get(name, default).split(","):
        try:
            out.append(float(s))
        except ValueError:
            pass
    return out


@dataclass
class AutoTradeConfig:
    enabled: bool = False
    mode: str = "dry_run"               # dry_run | mt5
    timeframe: str = "4h"
    allow_live: bool = False
    max_positions: int = 3              # distinct symbols; partial-TP tickets of one idea count once
    risk_percent: float = 0.5
    risk_base_balance: float = 0        # size as if the balance were this (huge demo accounts)
    risk_levels: list[float] = field(default_factory=lambda: [2, 3.5, 5])
    level2_confidence: float = 45
    level2_score: float = 28
    level3_confidence: float = 55
    level3_score: float = 40
    fixed_lots: float | None = None
    min_confidence: float = 40
    min_score: float = 20
    max_confidence: float = 0           # skip overextended reads above this (0 = off)
    tp_r: float = 0                     # fixed TP in R (0 = idea target)
    sl_mult: float = 1                  # widen the idea's stop
    flip_close: bool = True
    close_only_on_flip: bool = True
    magic: int = 260731
    deviation: int = 20
    exclude: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    style: str = "single"               # single | stack
    max_stacks: int = 1
    stack_step_pct: float = 0.08
    scalp_rr: float = 1.2
    partial_tps: bool = False
    partial_splits: list[float] = field(default_factory=lambda: [30, 25, 20, 15, 15])
    partial_rs: list[float] = field(default_factory=lambda: [0.5, 1, 1.5, 2, 0])
    max_hold_hours: float = 0
    manage: bool = True
    break_even_r: float = 0.5
    trail_start_r: float = 1.0
    trail_dist_r: float = 0.7
    max_margin_pct: float = 60
    confirm_trend: bool = True
    confirm_tf: str = "1h"
    max_spread_risk_pct: float = 30
    block_hours: list[int] = field(default_factory=lambda: [21, 22])
    feedback: bool = True
    feedback_days: int = 3
    pause_after_losses: int = 4
    pause_hours: float = 12
    symbols: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SYMBOLS))

    @staticmethod
    def from_env() -> "AutoTradeConfig":
        mode = os.environ.get("AUTO_TRADE_MODE", "dry_run").lower()
        hours = os.environ.get("AUTO_TRADE_BLOCK_HOURS", "21,22")
        return AutoTradeConfig(
            enabled=_bool("AUTO_TRADE_ENABLED", False),
            mode="mt5" if mode in ("mt5", "bridge", "live") else "dry_run",
            timeframe=os.environ.get("AUTO_TRADE_TIMEFRAME", "4h") if os.environ.get("AUTO_TRADE_TIMEFRAME", "4h") in TIMEFRAMES else "4h",
            allow_live=_bool("AUTO_TRADE_ALLOW_LIVE", False),
            max_positions=int(_num("AUTO_TRADE_MAX_POSITIONS", 3)),
            risk_percent=_num("AUTO_TRADE_RISK_PERCENT", 0.5),
            risk_base_balance=_num("AUTO_TRADE_RISK_BASE_BALANCE", 0),
            risk_levels=[_num("AUTO_TRADE_RISK_L1", 2), _num("AUTO_TRADE_RISK_L2", 3.5), _num("AUTO_TRADE_RISK_L3", 5)],
            level2_confidence=_num("AUTO_TRADE_L2_CONFIDENCE", 45), level2_score=_num("AUTO_TRADE_L2_SCORE", 28),
            level3_confidence=_num("AUTO_TRADE_L3_CONFIDENCE", 55), level3_score=_num("AUTO_TRADE_L3_SCORE", 40),
            fixed_lots=_num("AUTO_TRADE_FIXED_LOTS", 0) or None,
            min_confidence=_num("AUTO_TRADE_MIN_CONFIDENCE", 40), min_score=_num("AUTO_TRADE_MIN_SCORE", 20),
            max_confidence=_num("AUTO_TRADE_MAX_CONFIDENCE", 0),
            tp_r=_num("AUTO_TRADE_TP_R", 0), sl_mult=_num("AUTO_TRADE_SL_MULT", 1) or 1,
            flip_close=_bool("AUTO_TRADE_FLIP_CLOSE", True), close_only_on_flip=_bool("AUTO_TRADE_CLOSE_ONLY_ON_FLIP", True),
            magic=int(_num("AUTO_TRADE_MAGIC", 260731)), deviation=int(_num("AUTO_TRADE_DEVIATION", 20)),
            exclude=_list("AUTO_TRADE_EXCLUDE"), include=_list("AUTO_TRADE_INCLUDE"),
            style=os.environ.get("AUTO_TRADE_STYLE", "single").lower(),
            max_stacks=int(_num("AUTO_TRADE_MAX_STACKS", 1)), stack_step_pct=_num("AUTO_TRADE_STACK_STEP_PCT", 0.08),
            scalp_rr=_num("AUTO_TRADE_SCALP_RR", 1.2),
            partial_tps=_bool("AUTO_TRADE_PARTIAL_TPS", False),
            partial_splits=[x for x in _nums("AUTO_TRADE_PARTIAL_SPLITS", "30,25,20,15,15") if x > 0],
            partial_rs=_nums("AUTO_TRADE_PARTIAL_RS", "0.5,1,1.5,2,0"),
            max_hold_hours=_num("AUTO_TRADE_MAX_HOLD_HOURS", 0),
            manage=_bool("AUTO_TRADE_MANAGE", True), break_even_r=_num("AUTO_TRADE_BREAK_EVEN_R", 0.5),
            trail_start_r=_num("AUTO_TRADE_TRAIL_START_R", 1.0), trail_dist_r=_num("AUTO_TRADE_TRAIL_DIST_R", 0.7),
            max_margin_pct=_num("AUTO_TRADE_MAX_MARGIN_PCT", 60),
            confirm_trend=_bool("AUTO_TRADE_CONFIRM_TREND", True),
            confirm_tf=os.environ.get("AUTO_TRADE_CONFIRM_TF", "1h"),
            max_spread_risk_pct=_num("AUTO_TRADE_MAX_SPREAD_RISK_PCT", 30),
            block_hours=[int(h) for h in hours.split(",") if h.strip().isdigit() and 0 <= int(h) < 24],
            feedback=_bool("AUTO_TRADE_FEEDBACK", True), feedback_days=int(_num("AUTO_TRADE_FEEDBACK_DAYS", 3)),
            pause_after_losses=int(_num("AUTO_TRADE_PAUSE_AFTER_LOSSES", 4)), pause_hours=_num("AUTO_TRADE_PAUSE_HOURS", 12),
            symbols={k: os.environ.get(SYMBOL_ENV[k]) or v for k, v in DEFAULT_SYMBOLS.items()},
        )


def summarize_deals(deals: list[dict], days: int) -> dict:
    """MT5 deal legs -> closed round trips with win rate, net, profit factor, per-symbol / exit breakdowns."""
    by_pos: dict[int, list] = {}
    for d in deals or []:
        if d.get("symbol"):
            by_pos.setdefault(d["position_id"], []).append(d)
    closed = []
    for pid, rows in by_pos.items():
        rows.sort(key=lambda r: r["time"])
        ins = [r for r in rows if r["entry"] == 0]
        outs = [r for r in rows if r["entry"] in (1, 3)]
        if not outs:
            continue
        first, last = (ins or rows)[0], outs[-1]
        c = (last.get("comment") or "").lower()
        reason = ("take_profit" if "[tp" in c else "stop_loss" if "[sl" in c else "signal_close" if "mw-close" in c
                  else "stop_out" if "[so" in c else "manual_or_other")
        closed.append({"position_id": pid, "symbol": first["symbol"],
                       "side": ("buy" if first["type"] == 0 else "sell") if ins else None, "volume": first["volume"],
                       "open_price": first["price"] if ins else None, "close_price": last["price"],
                       "open_time": first["time"] if ins else None, "close_time": last["time"],
                       "hold_minutes": round((last["time"] - first["time"]) / 60) if ins else None,
                       "pnl": round(sum(r["profit"] + r["commission"] + r["swap"] + r["fee"] for r in rows), 2),
                       "exit_reason": reason, "comment": first.get("comment") or ""})
    closed.sort(key=lambda c: c["close_time"])
    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] < 0]
    gw, gl = sum(c["pnl"] for c in wins), sum(c["pnl"] for c in losses)
    by_sym: dict[str, dict] = {}
    by_exit: dict[str, dict] = {}
    for c in closed:
        s = by_sym.setdefault(c["symbol"], {"trades": 0, "wins": 0, "pnl": 0.0})
        s["trades"] += 1
        s["wins"] += c["pnl"] > 0
        s["pnl"] = round(s["pnl"] + c["pnl"], 2)
        e = by_exit.setdefault(c["exit_reason"], {"trades": 0, "pnl": 0.0})
        e["trades"] += 1
        e["pnl"] = round(e["pnl"] + c["pnl"], 2)
    return {"days": days, "closed_trades": len(closed), "wins": len(wins), "losses": len(losses),
            "win_rate_pct": round(100 * len(wins) / len(closed), 2) if closed else None,
            "net_profit": round(sum(c["pnl"] for c in closed), 2), "gross_win": round(gw, 2), "gross_loss": round(gl, 2),
            "profit_factor": round(abs(gw / gl), 2) if gl else None,
            "avg_hold_minutes": round(sum(c["hold_minutes"] or 0 for c in closed) / len(closed)) if closed else None,
            "by_symbol": by_sym, "by_exit_reason": by_exit, "recent": list(reversed(closed[-20:])), "closed": closed}


class AutoTrader:
    def __init__(self, cfg: AutoTradeConfig, market, mt=None, news_blackout=None) -> None:
        self.cfg = cfg
        self.market = market                # MarketService (candles for the trend check, news config)
        self.mt = mt                        # MT5Multi (or a fake in tests); None in dry_run
        self.news_blackout = news_blackout or (lambda asset_id: None)
        self.actions: list[dict] = []
        self.last_run: float | None = None
        self.error: str | None = None
        self.account: dict | None = None
        self.mode = "off"
        self.paused: list[dict] = []
        self._feedback: tuple[float, dict] = (0.0, {})
        self._initial_risk: dict[int, float] = {}
        self._cooldown: dict[str, float] = {}

    # --------------------------------------------------------------- status

    def status(self) -> dict:
        c = self.cfg
        return {"enabled": c.enabled, "mode": self.mode if c.enabled else "off", "timeframe": c.timeframe,
                "account": self.account, "last_run": self.last_run, "error": self.error,
                "max_positions": c.max_positions, "risk_levels": c.risk_levels, "style": c.style,
                "confirm_tf": self._confirm_tf() if c.confirm_trend else None, "exclude": c.exclude,
                "block_hours_utc": c.block_hours, "partial_tps": self._tp_levels(),
                "max_hold_hours": c.max_hold_hours or None, "paused": self.paused, "actions": self.actions[:15]}

    def _act(self, **a) -> None:
        a["at"] = time.time()
        self.actions.insert(0, a)
        del self.actions[40:]
        log.info("autotrade %s", {k: v for k, v in a.items() if k != "at"})

    # -------------------------------------------------------------- intents

    def symbol_for(self, asset_id: str) -> str | None:
        aid = asset_id.lower()
        if aid == "gold" or aid in self.cfg.exclude:           # gold belongs to the gold engine
            return None
        if self.cfg.include and aid not in self.cfg.include:
            return None
        return self.cfg.symbols.get(aid)

    def level(self, confidence: float, score: float) -> int:
        c = self.cfg
        if confidence >= c.level3_confidence and abs(score) >= c.level3_score:
            return 3
        if confidence >= c.level2_confidence and abs(score) >= c.level2_score:
            return 2
        return 1

    def risk_for(self, level: int) -> float:
        lv = self.cfg.risk_levels
        pct = lv[min(max(level, 1), 3) - 1] if len(lv) >= 3 else 0
        return pct if pct > 0 else self.cfg.risk_percent

    def intent(self, asset: dict) -> dict | None:
        c, sig = self.cfg, asset["signal"]
        tr = sig.get("trade") or {}
        if not tr.get("active") or tr.get("side") not in ("buy", "sell") or asset.get("demo_price"):
            return None
        symbol = self.symbol_for(asset["id"])
        if not symbol or sig["confidence"] < c.min_confidence or abs(sig["composite"]) < c.min_score:
            return None
        if c.max_confidence > 0 and sig["confidence"] > c.max_confidence:
            return None
        stop, target, entry = tr["stop"], tr["target"], tr["entry"]
        if c.sl_mult != 1 and entry is not None and stop is not None:
            r0 = abs(entry - stop)
            stop = entry - r0 * c.sl_mult if tr["side"] == "buy" else entry + r0 * c.sl_mult
        rr = c.tp_r if c.tp_r > 0 else (c.scalp_rr if c.style == "stack" else 0)
        if rr > 0 and entry is not None and stop is not None and abs(entry - stop) > 0:
            risk = abs(entry - stop)
            target = entry + risk * rr if tr["side"] == "buy" else entry - risk * rr
        lv = self.level(sig["confidence"], sig["composite"])
        return {"asset_id": asset["id"], "symbol": symbol, "side": tr["side"], "entry": entry, "stop": stop,
                "target": target, "confidence": sig["confidence"], "score": sig["composite"], "level": lv,
                "risk_percent": self.risk_for(lv), "comment": f"MW-L{lv}-{asset['id']}-{tr['side']}"[:31],
                "price": asset["price"]}

    def _tp_levels(self) -> list[dict] | None:
        c = self.cfg
        if not c.partial_tps or not c.partial_splits or len(c.partial_splits) != len(c.partial_rs):
            return None
        return [{"part": p, "r": r} for p, r in zip(c.partial_splits, c.partial_rs)]

    def _confirm_tf(self) -> str:
        tf = self.cfg.confirm_tf if self.cfg.confirm_tf in TIMEFRAMES else "1h"
        if tf == self.cfg.timeframe:
            tf = "1d" if tf == "4h" else "4h"
        return tf

    def htf_bias(self, intent: dict) -> int:
        """+1 up, -1 down, 0 mixed on the confirmation timeframe."""
        tf = TIMEFRAMES[self._confirm_tf()]
        if self.mt is not None:
            series = self.mt.candles(intent["symbol"], tf["id"], tf["points"])
        else:
            asset = next(a for a in ASSETS if a["id"] == intent["asset_id"])
            series = self.market.fetch(asset, tf)[0]
        ind = compute(series["candles"], with_series=False)
        if ind["ema20"] is None or ind["ema50"] is None or ind["sma50"] is None:
            return 0
        if ind["ema20"] > ind["ema50"] and ind["price"] > ind["sma50"]:
            return 1
        if ind["ema20"] < ind["ema50"] and ind["price"] < ind["sma50"]:
            return -1
        return 0

    # ------------------------------------------------------------- feedback

    def feedback(self) -> dict:
        if not self.cfg.feedback or self.mt is None:
            return {}
        at, data = self._feedback
        if time.time() - at < 600:
            return data
        closed = summarize_deals(self.mt.deals(self.cfg.feedback_days, self.cfg.magic), self.cfg.feedback_days)["closed"]
        by: dict[str, dict] = {}
        for t in closed:
            s = by.setdefault(t["symbol"], {"trades": 0, "net": 0.0, "streak": 0, "last": None})
            s["trades"] += 1
            s["net"] += t["pnl"]
            s["streak"] = s["streak"] + 1 if t["pnl"] < 0 else 0
            s["last"] = t["close_time"]
        for s in by.values():
            since = time.time() - s["last"] if s["last"] else float("inf")
            s["paused"] = s["streak"] >= self.cfg.pause_after_losses and since < self.cfg.pause_hours * 3600
            s["demoted"] = not s["paused"] and s["net"] < 0 and s["trades"] >= 5
            s["net"] = round(s["net"], 2)
        self.paused = [{"symbol": k, "losses": v["streak"], "last_close": v["last"]} for k, v in by.items() if v["paused"]]
        self._feedback = (time.time(), by)
        return by

    # ----------------------------------------------------------- management

    def _risk_distance(self, pos: dict) -> float:
        if pos["ticket"] in self._initial_risk:
            return self._initial_risk[pos["ticket"]]
        o, sl, tp = pos["price_open"], pos.get("sl") or 0, pos.get("tp") or 0
        risk = 0.0
        if sl > 0:
            d = o - sl if pos["type"] == "buy" else sl - o
            risk = d if d > 0 else 0.0                 # SL still on the losing side = the true initial risk
        if not risk and tp > 0:
            risk = abs(tp - o) / 1.5                   # restarted mid-trade with SL past entry
        risk = risk or o * 0.002
        self._initial_risk[pos["ticket"]] = risk
        if len(self._initial_risk) > 500:
            self._initial_risk.pop(next(iter(self._initial_risk)))
        return risk

    def manage(self, positions: list[dict], magics: set[int]) -> None:
        c = self.cfg
        if not c.manage:
            return
        for pos in positions:
            if pos["magic"] not in magics:
                continue
            side, o, cur = pos["type"], pos["price_open"], pos["price_current"]
            if not (o > 0 and cur > 0):
                continue
            sl = pos.get("sl") or 0
            if c.max_hold_hours and pos.get("time") and (time.time() - pos["time"]) / 3600 >= c.max_hold_hours:
                if not (sl > 0 and (sl >= o if side == "buy" else sl <= o)):      # leave risk-free runners
                    try:
                        self.mt.close(pos["ticket"])
                        self._cooldown[pos["symbol"]] = time.time() + 12 * 3600
                        self._act(type="close", reason="time_stop", symbol=pos["symbol"], ticket=pos["ticket"])
                    except Exception as ex:
                        log.warning("time stop %s failed: %s", pos["ticket"], ex)
                    continue
            risk = self._risk_distance(pos)
            prof_r = (cur - o if side == "buy" else o - cur) / risk
            new_sl = reason = None
            if prof_r >= c.trail_start_r:
                cand = cur - risk * c.trail_dist_r if side == "buy" else cur + risk * c.trail_dist_r
                if sl == 0 or (cand - sl if side == "buy" else sl - cand) > risk * 0.1:
                    new_sl, reason = cand, "trail"
            elif prof_r >= c.break_even_r and (sl == 0 or (sl < o if side == "buy" else sl > o)):
                new_sl, reason = (o + risk * 0.05 if side == "buy" else o - risk * 0.05), "break_even"
            if new_sl is None:
                continue
            try:
                res = self.mt.modify(pos["ticket"], new_sl, pos.get("tp") or 0)
                self._act(type="modify", reason=reason, symbol=pos["symbol"], ticket=pos["ticket"],
                          sl=res.get("sl", new_sl), profit_r=round(prof_r, 2))
            except Exception as ex:
                log.warning("modify %s failed: %s", pos["ticket"], ex)

    # ------------------------------------------------------------------ run

    def run(self, snap: dict) -> None:
        c = self.cfg
        if not c.enabled or snap["timeframe"]["id"] != c.timeframe:
            return
        if getattr(getattr(self.market, "trading", None), "automation_paused", False):
            self.mode = "paused"
            return
        self.last_run, self.error = time.time(), None
        intents = sorted(filter(None, (self.intent(a) for a in snap["assets"])),
                         key=lambda i: (-i["confidence"], -abs(i["score"])))[:c.max_positions]
        if c.mode == "dry_run" or self.mt is None:
            self.mode = "dry_run"
            for i in intents:
                self._act(type="would_open", symbol=i["symbol"], side=i["side"], stop=i["stop"], target=i["target"],
                          confidence=i["confidence"], level=i["level"])
            return
        try:
            self.account = self.mt.account()
            self.mode = "demo" if self.account["is_demo"] else ("live" if c.allow_live else "blocked_live")
            if self.mode == "blocked_live":
                self.error = "Live account: refusing to auto-trade (set AUTO_TRADE_ALLOW_LIVE=true to override)."
                return
            positions = self.mt.positions()
            self.manage(positions, {c.magic, *self.extra_magics})
            want = {i["symbol"]: i for i in intents}
            if c.flip_close:
                for pos in positions:
                    if pos["magic"] != c.magic:
                        continue
                    w = want.get(pos["symbol"])
                    flipped, stale = bool(w and w["side"] != pos["type"]), w is None
                    if flipped or (stale and not c.close_only_on_flip):
                        self.mt.close(pos["ticket"])
                        self._act(type="close", symbol=pos["symbol"], ticket=pos["ticket"],
                                  reason="flip" if flipped else "no_active_idea")
            live = [p for p in self.mt.positions() if p["magic"] == c.magic]
            hour = time.gmtime().tm_hour
            if intents and hour in c.block_hours:
                self._act(type="session_block", hour_utc=hour, skipped=[i["symbol"] for i in intents])
                return
            try:
                fb = self.feedback()
            except Exception as ex:
                log.warning("feedback stats unavailable: %s", ex)
                fb = {}
            for i in intents:
                self._enter(i, live, fb)
        except Exception as ex:
            self.error = str(ex)
            log.exception("autotrade failed")

    extra_magics: tuple[int, ...] = ()      # other strategies whose positions this manages too (Sweep+BOS)

    def _can_stack(self, stacks: list[dict], side: str, price: float) -> bool:
        if len(stacks) >= max(1, self.cfg.max_stacks):
            return False
        if not stacks:
            return True
        if self.cfg.style != "stack":
            return False
        last = max(stacks, key=lambda p: p["ticket"])["price_open"]
        step = self.cfg.stack_step_pct / 100
        return price <= last * (1 - step) if side == "buy" else price >= last * (1 + step)

    def _enter(self, i: dict, live: list[dict], fb: dict) -> None:
        c = self.cfg
        stacks = [p for p in live if p["symbol"] == i["symbol"] and p["type"] == i["side"]]
        price = i["price"] if i["price"] is not None else i["entry"]
        if not self._can_stack(stacks, i["side"], price):
            return
        symbols = {p["symbol"] for p in live}
        if i["symbol"] not in symbols and len(symbols) >= c.max_positions:
            return
        if self._cooldown.get(i["symbol"], 0) > time.time():
            return
        ev = self.news_blackout(i["asset_id"])
        if ev:
            self._act(type="skip", symbol=i["symbol"], side=i["side"], reason=f"news blackout: {ev['currency']} {ev['title']}")
            return
        f = fb.get(i["symbol"])
        if f and f["paused"]:
            self._act(type="skip", symbol=i["symbol"], side=i["side"],
                      reason=f"cooldown: {f['streak']} straight losses (net {f['net']})")
            return
        if f and f["demoted"] and i["level"] > 1:
            i.update(level=1, risk_percent=self.risk_for(1), comment=f"MW-L1-{i['asset_id']}-{i['side']}"[:31])
        if c.confirm_trend:
            try:
                bias = self.htf_bias(i)
                if (i["side"] == "buy" and bias < 0) or (i["side"] == "sell" and bias > 0):
                    self._act(type="skip", symbol=i["symbol"], side=i["side"], reason=f"against {self._confirm_tf()} trend")
                    return
            except Exception as ex:
                log.warning("trend check failed for %s: %s; allowing", i["symbol"], ex)
        try:
            res = self.mt.order(i["symbol"], i["side"], i["stop"], i["target"], entry=i["entry"],
                                risk_percent=i["risk_percent"], volume=c.fixed_lots, risk_base_balance=c.risk_base_balance,
                                max_margin_pct=c.max_margin_pct, max_spread_risk_pct=c.max_spread_risk_pct,
                                tp_levels=self._tp_levels(), magic=c.magic, deviation=c.deviation,
                                comment=(i["comment"] + (f"-s{len(stacks) + 1}" if stacks else ""))[:31])
            self._act(type="stack" if stacks else "open", symbol=i["symbol"], side=i["side"], level=i["level"],
                      volume=res.get("volume"), sl=res.get("sl"), tp=res.get("tp"))
            live.append({"magic": c.magic, "symbol": i["symbol"], "type": i["side"], "price_open": price,
                         "ticket": int(time.time() * 1000)})
        except Exception as ex:          # one rejected order shouldn't block the rest
            self._act(type="skip", symbol=i["symbol"], side=i["side"], reason=str(ex))
