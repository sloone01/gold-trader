"""Regime-aware composite signal (-100..+100) and the buy/sell trade idea for one asset.

Rule-based technical analysis, explained in English and Arabic. Not financial advice.
"""
from __future__ import annotations

from .indicators import compute, detect_regime

SIGNAL_EN = {"bullish": "bullish", "bearish": "bearish", "neutral": "neutral"}
SIGNAL_AR = {"bullish": "صاعد", "bearish": "هابط", "neutral": "محايد"}

DEFAULT_TECH_WEIGHTS = {
    "trending": {"trend": 0.38, "rsi": 0.14, "macd": 0.36, "bollinger": 0.12},
    "ranging": {"trend": 0.16, "rsi": 0.34, "macd": 0.18, "bollinger": 0.32},
}
NEWS_WEIGHT = 0.12


def with_news(tech: dict) -> dict:
    scale = 1 - NEWS_WEIGHT
    return {**{k: tech[k] * scale for k in ("trend", "rsi", "macd", "bollinger")}, "news": NEWS_WEIGHT}


DEFAULT_WEIGHTS = {r: with_news(w) for r, w in DEFAULT_TECH_WEIGHTS.items()}


def fmt(n: float | None) -> str:
    if n is None:
        return "—"
    if n >= 1000:
        return f"{n:,.2f}".rstrip("0").rstrip(".")
    if n >= 1:
        return f"{n:.2f}"
    return f"{n:.4f}"


def _for_ar(name: str) -> str:
    return "لل" + name[2:] if name.startswith("ال") else "لـ " + name


def classify(s: float) -> str:
    return "bullish" if s >= 0.25 else "bearish" if s <= -0.25 else "neutral"


def _rule(score: float, note: str, note_ar: str) -> dict:
    return {"score": score, "label": classify(score), "note": note, "note_ar": note_ar}


def trend_rule(ind: dict) -> dict | None:
    price, sma50, ema20, ema50 = ind["price"], ind["sma50"], ind["ema20"], ind["ema50"]
    if sma50 is None or ema20 is None or ema50 is None:
        return None
    above, ema_up = price > sma50, ema20 > ema50
    score = (0.5 if above else -0.5) + (0.5 if ema_up else -0.5)
    return _rule(score,
                 f"Price is {'above' if above else 'below'} its 50-period average; "
                 f"the 20-period EMA is {'above' if ema_up else 'below'} the 50-period EMA.",
                 f"السعر {'أعلى من' if above else 'أدنى من'} متوسطه المتحرك لـ 50 فترة، "
                 f"والمتوسط الأسي لـ 20 فترة {'أعلى من' if ema_up else 'أدنى من'} المتوسط الأسي لـ 50 فترة.")


def rsi_rule(ind: dict, regime: str) -> dict | None:
    r = ind["rsi14"]
    if r is None:
        return None
    v = f"{r:.0f}"
    if regime == "trending":
        if r >= 55:
            return _rule(0.45, f"RSI {v} — momentum confirms the upside in a trending regime.",
                         f"مؤشر القوة النسبية {v} — الزخم يؤكد الاتجاه الصاعد في نظام اتجاهي.")
        if r <= 45:
            return _rule(-0.45, f"RSI {v} — momentum confirms the downside in a trending regime.",
                         f"مؤشر القوة النسبية {v} — الزخم يؤكد الاتجاه الهابط في نظام اتجاهي.")
        return _rule(0, f"RSI {v} — neutral momentum.", f"مؤشر القوة النسبية {v} — زخم محايد.")
    if r >= 70:
        return _rule(-0.7, f"RSI {v} — overbought (mean-reversion setup in a range).",
                     f"مؤشر القوة النسبية {v} — تشبّع شرائي (إعداد ارتداد في نطاق).")
    if r <= 30:
        return _rule(0.7, f"RSI {v} — oversold (mean-reversion setup in a range).",
                     f"مؤشر القوة النسبية {v} — تشبّع بيعي (إعداد ارتداد في نطاق).")
    if r >= 55:
        return _rule(0.25, f"RSI {v} — momentum leans positive.", f"مؤشر القوة النسبية {v} — الزخم يميل إيجابيًا.")
    if r <= 45:
        return _rule(-0.25, f"RSI {v} — momentum leans negative.", f"مؤشر القوة النسبية {v} — الزخم يميل سلبيًا.")
    return _rule(0, f"RSI {v} — neutral momentum.", f"مؤشر القوة النسبية {v} — زخم محايد.")


def macd_rule(ind: dict) -> dict | None:
    m = ind["macd"]
    if not m:
        return None
    price = ind["price"] or 1
    mag = min(1.0, abs(m["histogram"]) / (price * 0.002 + 1e-9))
    score = (1 if m["histogram"] > 0 else -1) * (0.35 + 0.45 * mag)
    if m["crossed_up"]:
        score = max(score, 0.85)
    if m["crossed_down"]:
        score = min(score, -0.85)
    pos, above = m["histogram"] > 0, m["macd"] > m["signal"]
    note = (f"MACD histogram is {'positive' if pos else 'negative'} "
            f"(MACD line {'above' if above else 'below'} its signal line)")
    note_ar = f"هيستوغرام الماكد {'موجب' if pos else 'سالب'} (خط الماكد {'أعلى من' if above else 'أدنى من'} خط الإشارة)"
    if m["crossed_up"]:
        note, note_ar = note + "; bullish cross just fired", note_ar + "؛ تقاطع صاعد للتو"
    elif m["crossed_down"]:
        note, note_ar = note + "; bearish cross just fired", note_ar + "؛ تقاطع هابط للتو"
    return _rule(score, note + ".", note_ar + ".")


def bollinger_rule(ind: dict, regime: str) -> dict | None:
    b = ind["bollinger"]
    if not b:
        return None
    price = ind["price"]
    inside = ("Price is trading within the Bollinger Bands.", "السعر يتداول داخل نطاقات بولينجر.")
    if regime == "trending":
        if price > b["upper"]:
            return _rule(0.35, "Price is above the upper Bollinger Band — trend extension.",
                         "السعر أعلى من نطاق بولينجر العلوي — امتداد اتجاهي.")
        if price < b["lower"]:
            return _rule(-0.35, "Price is below the lower Bollinger Band — trend extension lower.",
                         "السعر أدنى من نطاق بولينجر السفلي — امتداد هابط.")
        pos = (price - b["lower"]) / ((b["upper"] - b["lower"]) or 1)
        return _rule((pos - 0.5) * 0.4, *inside)
    if price > b["upper"]:
        return _rule(-0.55, "Price is above the upper Bollinger Band — extended in a range.",
                     "السعر أعلى من نطاق بولينجر العلوي — ممتد داخل نطاق.")
    if price < b["lower"]:
        return _rule(0.55, "Price is below the lower Bollinger Band — extended in a range.",
                     "السعر أدنى من نطاق بولينجر السفلي — ممتد داخل نطاق.")
    return _rule(0, *inside)


def news_rule(news: dict | None) -> dict | None:
    arts = (news or {}).get("articles") or []
    if not arts:
        return None
    avg = sum(a.get("sentiment") or 0 for a in arts) / len(arts)
    return _rule(max(-1.0, min(1.0, avg * 1.5)),
                 f"Average sentiment across {len(arts)} recent headline(s): {avg:.2f}.",
                 f"متوسط المشاعر عبر {len(arts)} عنوان إخباري حديث: {avg:.2f}.")


def score_components(ind: dict, news: dict | None, regime: str) -> dict:
    return {"trend": trend_rule(ind), "rsi": rsi_rule(ind, regime), "macd": macd_rule(ind),
            "bollinger": bollinger_rule(ind, regime), "news": news_rule(news)}


def composite_from(components: dict, weights: dict) -> float:
    w = tw = 0.0
    for k, c in components.items():
        if c and weights.get(k) is not None:
            w += c["score"] * weights[k]
            tw += weights[k]
    return w / tw if tw else 0.0


def confidence_of(components: dict, composite: float, regime: dict) -> int:
    active = [c for c in components.values() if c]
    if not active:
        return 0
    label = classify(composite)
    if label == "neutral":
        agreement = sum(1 for c in active if c["label"] == "neutral") / len(active)
    else:
        agreement = sum(1 for c in active if c["label"] == label) / len(active)
    strength = min(1.0, abs(composite))
    if regime["adx"] is None:
        boost = 0.5
    elif regime["regime"] == "trending":
        boost = min(1.0, regime["adx"] / 40)
    else:
        boost = min(1.0, (25 - min(regime["adx"], 25)) / 25)
    return round((0.45 * agreement + 0.35 * strength + 0.2 * boost) * 100)


def walk_forward_hit_rate(candles: list[dict], weights_for=None, min_bars: int = 60) -> dict | None:
    """How often a |score| >= 25 read called the next bar's direction, replayed on past bars."""
    if len(candles) < min_bars + 10:
        return None
    hits = total = 0
    step = max(1, (len(candles) - min_bars) // 40)
    for i in range(min_bars, len(candles) - 1, step):
        ind = compute(candles[:i + 1], with_series=False)
        reg = detect_regime(ind)["regime"]
        w = weights_for(reg) if weights_for else DEFAULT_WEIGHTS[reg]
        score = composite_from(score_components(ind, None, reg), w)
        if abs(score) < 0.25:
            continue
        nxt = candles[i + 1]["close"] - candles[i]["close"]
        hits += (score > 0 and nxt > 0) or (score < 0 and nxt < 0)
        total += 1
    if total < 5:
        return None
    return {"rate": round(hits / total * 100), "samples": total, "hits": hits}


def round_px(n: float | None, price: float) -> float | None:
    if n is None:
        return None
    return round(n, 2 if price >= 1000 else 4 if price >= 1 else 6)


def build_trade_idea(ind: dict, score100: int, confidence: int) -> dict:
    """Buy/sell sketch from the composite, swing levels and ATR. Shorts are first-class."""
    price, atr_v, lv = ind["price"], ind["atr14"], ind["levels"]
    if price is None:
        return {"side": None, "active": False}
    side = None
    if confidence >= 35 and score100 >= 20:
        side = "buy"
    elif confidence >= 35 and score100 <= -20:
        side = "sell"
    if not side:
        lean = "sell" if score100 <= -10 else "buy" if score100 >= 10 else None
        en = {"sell": "Sell lean, but below activation (|score|≥20 and confidence≥35%). Watching for a fuller short setup.",
              "buy": "Buy lean, but below activation (|score|≥20 and confidence≥35%). Watching for a fuller long setup.",
              None: "Flat — no buy or sell position. Wait for a clearer directional read."}[lean]
        ar = {"sell": "ميل للبيع، لكن دون تفعيل الصفقة (|الدرجة|≥20 والثقة≥35%). بانتظار إعداد بيع أوضح.",
              "buy": "ميل للشراء، لكن دون تفعيل الصفقة (|الدرجة|≥20 والثقة≥35%). بانتظار إعداد شراء أوضح.",
              None: "محايد — لا صفقة شراء ولا بيع. انتظر قراءة اتجاهية أوضح."}[lean]
        return {"side": lean, "active": False, "reason": {"en": en, "ar": ar}}

    pad = atr_v * 1.5 if atr_v is not None else price * 0.008
    stretch = atr_v * 2.2 if atr_v is not None else price * 0.012
    entry = price
    sup, res = (lv or {}).get("support"), (lv or {}).get("resistance")
    if side == "buy":
        stop = sup if sup is not None and sup < price else price - pad
        target = res if res is not None and res > price else price + stretch
        risk = entry - stop
        if risk > 0 and target - entry < risk * 1.2:
            target = entry + risk * 1.5
    else:
        stop = res if res is not None and res > price else price + pad
        target = sup if sup is not None and sup < price else price - stretch
        risk = stop - entry
        if risk > 0 and entry - target < risk * 1.2:
            target = entry - risk * 1.5
    entry, stop, target = round_px(entry, price), round_px(stop, price), round_px(target, price)
    rr = round(abs(target - entry) / (abs(entry - stop) or 1e-9), 2)
    word, word_ar = ("BUY", "للشراء") if side == "buy" else ("SELL", "للبيع")
    return {
        "side": side, "active": True, "entry": entry, "stop": stop, "target": target, "risk_reward": rr,
        "atr": round_px(atr_v, price) if atr_v is not None else None,
        "note": {"en": f"{word} idea: enter near {fmt(entry)}, stop {fmt(stop)}, target {fmt(target)} (R:R ≈ {rr}).",
                 "ar": f"فكرة {word_ar}: دخول قرب {fmt(entry)}، وقف {fmt(stop)}، هدف {fmt(target)} (عائد/مخاطرة ≈ {rr})."},
    }


def _summary(asset: dict, ind: dict, comps: dict, composite: int, regime: dict, confidence: int,
             hit: dict | None, calibrated: bool) -> dict:
    label = classify(composite / 100)
    sign = "+" if composite > 0 else ""
    lv = ind["levels"]
    trending = regime["regime"] == "trending"
    en = [f"{asset['name']}'s technical posture is currently {SIGNAL_EN[label]} "
          f"(composite {sign}{composite}/100, confidence {confidence}%). "
          f"Market regime: {'trending' if trending else 'range-bound'} ({regime['reason']})."
          + (" Weights are walk-forward calibrated." if calibrated else "")]
    ar = [f"الوضع الفني {_for_ar(asset['name_ar'])} حاليًا {SIGNAL_AR[label]} "
          f"(الدرجة {sign}{composite}/100، الثقة {confidence}%). "
          f"نظام السوق: {'اتجاهي' if trending else 'نطاقي'} ({regime['reason_ar']})."
          + (" الأوزان معايرة بأثر رجعي." if calibrated else "")]
    for k in ("trend", "rsi", "macd"):
        if comps[k]:
            en.append(comps[k]["note"])
            ar.append(comps[k]["note_ar"])
    if lv:
        en.append(f"Key levels: support near {fmt(lv['support'])}, resistance near {fmt(lv['resistance'])} ({lv['method']} pivots).")
        ar.append(f"مستويات مهمة: الدعم قرب {fmt(lv['support'])}، والمقاومة قرب {fmt(lv['resistance'])} "
                  f"(محاور {'أرجحية' if lv['method'] == 'swing' else 'نطاق'}).")
    if ind["atr14"] is not None:
        en.append(f"ATR(14) ≈ {fmt(ind['atr14'])}" + (f"; ADX(14) ≈ {fmt(ind['adx14'])}" if ind["adx14"] is not None else "") + ".")
        ar.append(f"المدى الحقيقي ≈ {fmt(ind['atr14'])}" + (f"؛ مؤشر الاتجاه ≈ {fmt(ind['adx14'])}" if ind["adx14"] is not None else "") + ".")
    if hit:
        en.append(f"Recent walk-forward hit-rate: {hit['rate']}% over {hit['samples']} signals (next-bar direction).")
        ar.append(f"معدل الإصابة الأخير: {hit['rate']}% عبر {hit['samples']} إشارة (اتجاه الشمعة التالية).")
    en.append("This is a technical read of the indicators — not a buy or sell recommendation.")
    ar.append("هذه قراءة فنية للمؤشرات — وليست توصية بالشراء أو البيع.")
    return {"en": " ".join(en), "ar": " ".join(ar)}


def build_signal(ind: dict, news: dict | None, asset: dict, candles: list[dict],
                 weights_for=None, calibrated: bool = False) -> dict:
    regime = detect_regime(ind)
    comps = score_components(ind, news, regime["regime"])
    weights = (weights_for or (lambda r: DEFAULT_WEIGHTS[r]))(regime["regime"])
    composite = composite_from(comps, weights)
    score100 = round(composite * 100)
    confidence = confidence_of(comps, composite, regime)
    hit = walk_forward_hit_rate(candles, weights_for) if candles else None
    label = classify(composite)
    active = [c for c in comps.values() if c]
    return {
        "components": comps, "composite": score100, "label": label, "confidence": confidence,
        "agreement": {"aligned": sum(1 for c in active if c["label"] == label and label != "neutral"), "total": len(active)},
        "regime": regime, "weights": weights, "calibrated": calibrated, "hit_rate": hit,
        "trade": build_trade_idea(ind, score100, confidence),
        "summary": _summary(asset, ind, comps, score100, regime, confidence, hit, calibrated),
    }
