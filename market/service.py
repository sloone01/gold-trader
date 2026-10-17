"""Market Watch inside Gold Trader: refreshes every asset on every timeframe in the background,
builds the signals and trade ideas for the dashboard, alerts on signal flips, and sends the gold
idea to the signal service as the "Market Watch" channel so the engine manages the trade.
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

from engine import Event

from . import providers as pv
from .calibrate import Weights
from .config import ASSETS, DEFAULT_TIMEFRAME, TIMEFRAMES, MarketConfig
from .indicators import compute
from .signals import build_signal

log = logging.getLogger("gold-trader.market")
FEED = "market-watch"


class MarketService:
    def __init__(self, cfg: MarketConfig, trading=None, signals=None, fetch=None) -> None:
        self.cfg = cfg
        self.trading = trading          # server.service.TradingService (broker candles for gold, events)
        self.signals = signals          # signals.SignalService (the gold feed)
        self.weights = Weights(trading.store if trading else None)
        self.fetch = fetch or self._fetch   # (asset, tf) -> (series dict, demo?); tests swap it
        self.snapshots: dict[str, dict] = {}
        self.flips: list[dict] = []
        self._last_labels: dict[str, dict[str, str]] = {}
        self.feed = GoldFeed(self)
        self.lock = threading.Lock()
        self._refreshing: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_manual = 0.0

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread or not self.cfg.enabled:
            return
        self._thread = threading.Thread(target=self._loop, name="market", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        order = sorted(self.cfg.timeframes, key=lambda t: t != DEFAULT_TIMEFRAME)   # default first
        while not self._stop.is_set():
            started = time.time()
            for tf in order:
                if self._stop.is_set():
                    return
                self.refresh(tf)
                self._stop.wait(8)          # stagger so free-tier quotas aren't burst
            self._stop.wait(max(5.0, self.cfg.refresh_seconds - (time.time() - started)))

    # ------------------------------------------------------------- snapshot

    def snapshot(self, tf: str) -> dict:
        tf = tf if tf in TIMEFRAMES else DEFAULT_TIMEFRAME
        snap = self.snapshots.get(tf)
        if snap is None and tf not in self._refreshing:
            snap = self.refresh(tf)
        return snap or self._empty(tf)

    def _empty(self, tf: str) -> dict:
        return {"generated_at": None, "status": "starting", "timeframe": _tf_view(TIMEFRAMES[tf]),
                "timeframes": [_tf_view(t) for t in TIMEFRAMES.values()], "assets": [], "triage": {},
                "flips": self.flips, "feed": self.feed.status()}

    def refresh(self, tf: str) -> dict:
        with self.lock:
            if tf in self._refreshing:
                return self.snapshots.get(tf) or self._empty(tf)
            self._refreshing.add(tf)
        try:
            snap = self._build(TIMEFRAMES[tf])
            self.snapshots[tf] = snap
            log.info("refreshed %s: %d assets%s", tf, len(snap["assets"]),
                     f", {len(snap['new_flips'])} flip(s)" if snap["new_flips"] else "")
            return snap
        except Exception as ex:
            log.exception("market refresh %s failed", tf)
            prev = self.snapshots.get(tf) or self._empty(tf)
            self.snapshots[tf] = {**prev, "status": "error", "error": str(ex)}
            return self.snapshots[tf]
        finally:
            self._refreshing.discard(tf)

    def _build(self, tf: dict) -> dict:
        rows = []
        for asset in ASSETS:
            series, is_demo = self.fetch(asset, tf)
            try:
                nw = pv.news(asset, self.cfg.marketaux_key)
            except Exception as ex:
                log.warning("%s news failed: %s", asset["id"], ex)
                nw = {"demo": True, "articles": []}
            rows.append((asset, series, is_demo, compute(series["candles"]), nw))

        live = [(a["kind"], s["candles"]) for a, s, d, _, _ in rows if not d and s["candles"]]
        calibrated = False
        if len(live) >= 2:
            self.weights.calibrate(live)
            calibrated = True

        assets = []
        for asset, series, is_demo, ind, nw in rows:
            sig = build_signal(ind, nw, asset, series["candles"],
                               weights_for=lambda r, k=asset["kind"]: self.weights.for_regime(r, k),
                               calibrated=calibrated)
            assets.append(_public(asset, series, is_demo, ind, nw, sig, tf["id"]))

        new_flips = self._detect_flips(tf["id"], assets)
        snap = {
            "generated_at": time.time(), "status": "ok", "timeframe": _tf_view(tf),
            "timeframes": [_tf_view(t) for t in TIMEFRAMES.values()],
            "calibration": {"updated_at": self.weights.data.get("updated_at"), "stats": self.weights.data.get("stats"),
                            "active": calibrated or bool(self.weights.data.get("updated_at"))},
            "triage": triage(assets), "assets": assets, "new_flips": new_flips,
        }
        if tf["id"] == self.cfg.feed_timeframe:
            self.feed.run(snap)
        snap["flips"], snap["feed"] = self.flips, self.feed.status()
        return snap

    def _fetch(self, asset: dict, tf: dict) -> tuple[dict, bool]:
        if asset["id"] == "gold" and self.trading is not None:
            try:
                return pv.broker_candles(self.trading.broker, tf), False
            except pv.FeedError:
                pass
            except Exception as ex:
                log.warning("broker candles failed: %s", ex)
        if asset["provider"] == "binance":
            try:
                return pv.binance(asset, tf), False
            except Exception as ex:
                log.warning("%s Binance failed: %s", asset["id"], ex)
        if asset["provider"] == "twelvedata":
            try:
                return pv.twelvedata(asset, tf, self.cfg.twelvedata_key), False
            except Exception as ex:
                if str(ex) != "NO_KEY":
                    log.warning("%s Twelve Data failed: %s, trying Yahoo", asset["id"], ex)
            try:
                return pv.yahoo(asset, tf), False
            except Exception as ex:
                log.warning("%s Yahoo failed: %s, using demo data", asset["id"], ex)
        return pv.demo(asset, tf), True

    # ---------------------------------------------------------------- flips

    def _detect_flips(self, tf: str, assets: list[dict]) -> list[dict]:
        prev = self._last_labels.setdefault(tf, {})
        out = []
        for a in assets:
            label = a["signal"]["label"]
            if a["id"] in prev and prev[a["id"]] != label:
                out.append({"id": a["id"], "name": a["name"], "name_ar": a["name_ar"], "from": prev[a["id"]], "to": label,
                            "composite": a["signal"]["composite"], "confidence": a["signal"]["confidence"],
                            "timeframe": tf, "at": time.time()})
            prev[a["id"]] = label
        if out:
            self.flips = (out + self.flips)[:20]
            self._alert(out)
        return out

    def _alert(self, flips: list[dict]) -> None:
        if self.trading:
            for f in flips:
                self.trading.publish(Event("info", 0, f"{f['name']} ({f['timeframe']}): {f['from']} → {f['to']} "
                                                       f"({f['composite']:+d}, conf {f['confidence']}%)", "Markets"))
        if not (self.cfg.alert_bot_token and self.cfg.alert_chat_id):
            return
        lines = [f"• {f['name']} ({f['timeframe']}): {f['from']} → {f['to']} ({f['composite']:+d}, conf {f['confidence']}%)"
                 for f in flips]
        try:
            httpx.post(f"https://api.telegram.org/bot{self.cfg.alert_bot_token}/sendMessage", timeout=10,
                       json={"chat_id": self.cfg.alert_chat_id, "disable_web_page_preview": True,
                             "text": "Market Watch — signal flip\n" + "\n".join(lines)})
        except Exception as ex:
            log.warning("Telegram alert failed: %s", ex)

    # --------------------------------------------------------------- manual

    def refresh_now(self, tf: str, min_interval: float = 15) -> dict:
        wait = min_interval - (time.time() - self.last_manual)
        if wait > 0:
            return {"status": "rate_limited", "retry_after": round(wait)}
        self.last_manual = time.time()
        snap = self.refresh(tf if tf in TIMEFRAMES else DEFAULT_TIMEFRAME)
        return {"status": snap["status"], "generated_at": snap["generated_at"]}


class GoldFeed:
    """Sends the gold idea to the signal service, one message per change: an open when an idea
    appears, a close when it flips (and, optionally, when it goes inactive). The signal service
    ignores a repeated open on a side the channel already holds, so a restart can't double up."""

    RETRY_S = 30 * 60

    def __init__(self, market: MarketService) -> None:
        self.m = market
        self.held: dict | None = None       # {side, key}
        self.retry_at = 0.0
        self.actions: list[dict] = []
        self.error: str | None = None
        self.last_run: float | None = None

    def status(self) -> dict:
        on = self.m.cfg.feed and self.m.signals is not None
        return {"enabled": on, "timeframe": self.m.cfg.feed_timeframe, "holding": self.held,
                "last_run": self.last_run, "error": self.error, "actions": self.actions[:10]}

    def _log(self, kind: str, side: str | None, status: str | None, reason: str | None = None) -> None:
        self.actions.insert(0, {"type": kind, "side": side, "status": status, "reason": reason, "at": time.time()})
        del self.actions[20:]
        log.info("feed %s %s: %s%s", kind, side or "", status, f" ({reason})" if reason else "")

    def idea(self, asset: dict | None) -> dict | None:
        tr = (asset or {}).get("signal", {}).get("trade") or {}
        if not tr.get("active") or tr.get("side") not in ("buy", "sell") or asset.get("demo_price"):
            return None
        sig = asset["signal"]
        if sig["confidence"] < self.m.cfg.feed_min_confidence or abs(sig["composite"]) < self.m.cfg.feed_min_score:
            return None
        return tr if tr.get("entry") is not None and tr.get("stop") is not None else None

    def targets(self, tr: dict) -> list[float]:
        rs = self.m.cfg.feed_tp_rs
        if not rs:
            return [tr["target"]] if tr.get("target") is not None else []
        risk, d = abs(tr["entry"] - tr["stop"]), 1 if tr["side"] == "buy" else -1
        return [round(tr["entry"] + d * risk * r, 2) for r in rs]

    def _close(self, reason: str) -> None:
        sig = self.m.signals.ingest(FEED, f"{self.held['key']}:close", "close", note=f"Market Watch: {reason}")
        self._log("close", self.held["side"], sig.status if sig else "channel_off", reason)
        self.held = None

    def run(self, snap: dict) -> None:
        if not self.m.cfg.feed or self.m.signals is None:
            return
        self.last_run = time.time()
        try:
            asset = next((a for a in snap["assets"] if a["id"] == "gold"), None)
            tr = self.idea(asset)
            if self.held and tr and tr["side"] != self.held["side"]:
                self._close(f"flipped to {tr['side']}")
            elif self.held and not tr and self.m.cfg.feed_close_on_inactive:
                self._close("idea no longer active")
            if tr and not self.held and time.time() >= self.retry_at:
                ev = pv.news_blackout("gold", self.m.cfg)
                if ev:
                    self._log("skip", tr["side"], None, f"news blackout: {ev['currency']} {ev['title']}")
                else:
                    self._open(asset, tr, snap["timeframe"]["id"])
            self.error = None
        except Exception as ex:
            self.error = str(ex)
            log.exception("gold feed failed")

    def _open(self, asset: dict, tr: dict, tf: str) -> None:
        key = f"{tf}-{tr['side']}-{int(time.time() * 1000)}"
        tps = self.targets(tr)
        s = asset["signal"]
        note = (f"Market Watch {tf}: {tr['side'].upper()} score {s['composite']:+d}, confidence {s['confidence']}% · "
                f"SL {tr['stop']}" + (f" · TP {' / '.join(f'{t:g}' for t in tps)}" if tps else ""))
        sig = self.m.signals.ingest(FEED, key, "open", side=tr["side"], entry=tr["entry"], sl=tr["stop"], tps=tps,
                                    note=note, title="Market Watch", confidence=min(1.0, s["confidence"] / 100),
                                    reanchor=asset["source"] != "mt5")
        status = sig.status if sig else "channel_off"
        if status in ("pending", "placed", "ignored"):   # ignored = already holding this side
            self.held = {"side": tr["side"], "key": key}
        else:
            self.retry_at = time.time() + self.RETRY_S
        self._log("open", tr["side"], status, sig.error if sig else None)


# ------------------------------------------------------------------ views

def _tf_view(tf: dict) -> dict:
    return {"id": tf["id"], "label": tf["label"], "label_ar": tf["label_ar"]}


def _public(asset: dict, series: dict, is_demo: bool, ind: dict, nw: dict, sig: dict, tf: str) -> dict:
    candles = series["candles"]
    prev = candles[-2]["close"] if len(candles) > 1 else series["price"]
    n = min(80, len(candles))
    s = ind["series"]
    return {
        "id": asset["id"], "name": asset["name"], "name_ar": asset["name_ar"], "kind": asset["kind"],
        "unit": asset["unit"], "price": series["price"],
        "change_pct": (series["price"] - prev) / prev * 100 if prev else 0.0,
        "demo_price": is_demo, "demo_news": nw.get("demo", False), "stale": is_demo or bool(series.get("stale")),
        "source": series.get("source") or ("demo" if is_demo else asset["provider"]), "timeframe": tf,
        "spark": [c["close"] for c in candles[-60:]],
        "chart": {"times": s["times"][-n:], "close": s["close"][-n:], "sma20": s["sma20"][-n:], "ema20": s["ema20"][-n:],
                  "bb_upper": s["bollinger"]["upper"][-n:], "bb_mid": s["bollinger"]["middle"][-n:],
                  "bb_lower": s["bollinger"]["lower"][-n:]},
        "indicators": {k: v for k, v in ind.items() if k != "series"},
        "signal": sig, "news": nw.get("articles") or [],
    }


def _brief(a: dict) -> dict:
    return {"id": a["id"], "name": a["name"], "name_ar": a["name_ar"], "composite": a["signal"]["composite"],
            "label": a["signal"]["label"], "confidence": a["signal"]["confidence"], "change_pct": a["change_pct"],
            "price": a["price"]}


def triage(assets: list[dict]) -> dict:
    scored = sorted(assets, key=lambda a: -a["signal"]["composite"])

    def side(s: str) -> list[dict]:
        rows = [a for a in assets if (a["signal"]["trade"] or {}).get("side") == s]
        rows.sort(key=lambda a: (not a["signal"]["trade"].get("active"), -a["signal"]["confidence"],
                                 -abs(a["signal"]["composite"])))
        out = []
        for a in rows[:4]:
            tr = a["signal"]["trade"]
            out.append({**_brief(a), "side": s, "active": bool(tr.get("active")), "entry": tr.get("entry"),
                        "stop": tr.get("stop"), "target": tr.get("target"), "risk_reward": tr.get("risk_reward")})
        return out
    return {
        "bullish": [_brief(a) for a in scored if a["signal"]["composite"] > 0][:3],
        "bearish": [_brief(a) for a in reversed(scored) if a["signal"]["composite"] < 0][:3],
        "movers": [_brief(a) for a in sorted(assets, key=lambda a: -abs(a["change_pct"]))[:3]],
        "buys": side("buy"), "sells": side("sell"),
    }
