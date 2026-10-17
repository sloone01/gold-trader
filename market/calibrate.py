"""Walk-forward weight calibration.

Picks the technical weights per regime (globally and per asset kind) that best called the
next bar's direction on recent history. News keeps its fixed weight (no news history).
The result is saved in the app's settings store so it survives restarts.
"""
from __future__ import annotations

import copy
import logging
import time

from .indicators import compute, detect_regime
from .signals import DEFAULT_TECH_WEIGHTS, score_components, with_news

log = logging.getLogger("gold-trader.market")
KEYS = ("trend", "rsi", "macd", "bollinger")


def empty_store() -> dict:
    return {"updated_at": None, "tech": {"global": copy.deepcopy(DEFAULT_TECH_WEIGHTS), "by_kind": {}}, "stats": {}}


class Weights:
    def __init__(self, settings=None) -> None:
        self.settings = settings            # engine store (get_setting / put_setting) or None
        saved = settings.get_setting("market_weights") if settings else None
        self.data = saved or empty_store()

    def for_regime(self, regime: str, kind: str | None = None) -> dict:
        tech = (((self.data["tech"].get("by_kind") or {}).get(kind) or {}).get(regime)
                or self.data["tech"]["global"].get(regime) or DEFAULT_TECH_WEIGHTS.get(regime)
                or DEFAULT_TECH_WEIGHTS["ranging"])
        return with_news(tech)

    def calibrate(self, series: list[tuple[str, list[dict]]]) -> dict:
        """series: [(asset kind, candles)]. Replaces the stored weights."""
        glob: dict[str, list] = {"trending": [], "ranging": []}
        by_kind: dict[str, dict[str, list]] = {}
        for kind, candles in series:
            for s in collect_samples(candles):
                glob[s["regime"]].append(s)
                by_kind.setdefault(kind, {"trending": [], "ranging": []})[s["regime"]].append(s)
        nxt = {"updated_at": time.time(), "tech": {"global": copy.deepcopy(DEFAULT_TECH_WEIGHTS), "by_kind": {}}, "stats": {}}
        for regime in ("trending", "ranging"):
            if len(glob[regime]) >= 12:
                res = optimize(glob[regime], DEFAULT_TECH_WEIGHTS[regime])
                nxt["tech"]["global"][regime] = res["weights"]
                nxt["stats"][f"global:{regime}"] = _stats(res)
        for kind, regimes in by_kind.items():
            nxt["tech"]["by_kind"][kind] = {}
            for regime in ("trending", "ranging"):
                if len(regimes[regime]) < 16:   # fewer samples overfit (e.g. 8 -> 88%)
                    continue
                res = optimize(regimes[regime], DEFAULT_TECH_WEIGHTS[regime])
                nxt["tech"]["by_kind"][kind][regime] = res["weights"]
                nxt["stats"][f"{kind}:{regime}"] = _stats(res)
        self.data = nxt
        if self.settings:
            self.settings.put_setting("market_weights", nxt)
        log.info("calibrated: %s", "; ".join(f"{k} {v['hit_rate_before']}->{v['hit_rate_after']}% (n={v['samples']})"
                                             for k, v in nxt["stats"].items()) or "insufficient samples")
        return nxt


def _stats(res: dict) -> dict:
    return {"samples": res["after"]["samples"], "hit_rate_before": round(res["before"]["rate"] * 100),
            "hit_rate_after": round(res["after"]["rate"] * 100)}


def collect_samples(candles: list[dict], min_bars: int = 60) -> list[dict]:
    if not candles or len(candles) < min_bars + 10:
        return []
    out = []
    step = max(1, (len(candles) - min_bars) // 50)
    for i in range(min_bars, len(candles) - 1, step):
        ind = compute(candles[:i + 1], with_series=False)
        regime = detect_regime(ind)["regime"]
        scores = {k: c["score"] for k, c in score_components(ind, None, regime).items() if c and k != "news"}
        if len(scores) < 2:
            continue
        nxt = candles[i + 1]["close"] - candles[i]["close"]
        if nxt == 0:
            continue
        out.append({"regime": regime, "scores": scores, "up": nxt > 0})
    return out


def hit_rate(samples: list[dict], weights: dict, threshold: float = 0.25) -> dict:
    hits = total = 0
    for s in samples:
        w = tw = 0.0
        for k, score in s["scores"].items():
            if weights.get(k) is None:
                continue
            w += score * weights[k]
            tw += weights[k]
        if not tw or abs(w / tw) < threshold:
            continue
        hits += (w / tw > 0) == s["up"]
        total += 1
    if total < 5:
        return {"rate": 0.0, "samples": total, "hits": 0}
    return {"rate": hits / total, "samples": total, "hits": hits}


def candidates() -> list[dict]:
    out = []
    for a in range(11):
        for b in range(11 - a):
            for c in range(11 - a - b):
                d = 10 - a - b - c
                w = dict(zip(KEYS, (a / 10, b / 10, c / 10, d / 10)))
                if sum(1 for v in w.values() if v >= 0.1) >= 2:
                    out.append(w)
    return out


_CANDIDATES = candidates()


def optimize(samples: list[dict], baseline: dict) -> dict:
    best, best_stats = dict(baseline), hit_rate(samples, baseline)
    best_score = best_stats["rate"] + min(best_stats["samples"], 40) * 0.0005
    for w in _CANDIDATES:
        st = hit_rate(samples, w)
        if st["samples"] < 5:
            continue
        score = st["rate"] + min(st["samples"], 40) * 0.0005 - (0.01 if max(w.values()) > 0.7 else 0)
        if score > best_score:
            best, best_stats, best_score = w, st, score
    return {"weights": best, "before": hit_rate(samples, baseline), "after": best_stats}
