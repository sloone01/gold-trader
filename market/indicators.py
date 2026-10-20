"""Technical indicators. Lists of numbers or candles, oldest first. Pure functions.

A candle is a dict: {time, open, high, low, close}.
"""
from __future__ import annotations

import math


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def sma_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(values: list[float], period: int) -> float | None:
    s = ema_series(values, period)
    return s[-1] if s else None


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d >= 0:
            gain += d
        else:
            loss -= d
    avg_gain, avg_loss = gain / period, loss / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    if len(values) < slow + signal:
        return None
    fe, se = ema_series(values, fast), ema_series(values, slow)
    line = [f - s for f, s in zip(fe, se)]
    sig = ema_series(line, signal)
    hist = line[-1] - sig[-1]
    prev = line[-2] - sig[-2] if len(line) > 1 else 0.0
    return {"macd": line[-1], "signal": sig[-1], "histogram": hist,
            "crossed_up": prev <= 0 < hist, "crossed_down": prev >= 0 > hist}


def _band(window: list[float], mult: float) -> tuple[float, float, float]:
    mid = sum(window) / len(window)
    sd = math.sqrt(sum((v - mid) ** 2 for v in window) / len(window))
    return mid + mult * sd, mid, mid - mult * sd


def bollinger(values: list[float], period: int = 20, mult: float = 2) -> dict | None:
    if len(values) < period:
        return None
    up, mid, lo = _band(values[-period:], mult)
    return {"upper": up, "middle": mid, "lower": lo}


def bollinger_series(values: list[float], period: int = 20, mult: float = 2) -> dict:
    n = len(values)
    upper: list[float | None] = [None] * n
    middle: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n
    for i in range(period - 1, n):
        upper[i], middle[i], lower[i] = _band(values[i - period + 1:i + 1], mult)
    return {"upper": upper, "middle": middle, "lower": lower}


def _true_ranges(candles: list[dict]) -> list[float]:
    return [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
            for p, c in zip(candles, candles[1:])]


def atr(candles: list[dict], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    return sma(_true_ranges(candles), period)


def adx(candles: list[dict], period: int = 14) -> float | None:
    """Average Directional Index (Wilder), 0-100: higher = stronger trend."""
    if len(candles) < period * 2:
        return None
    plus_dm, minus_dm = [], []
    for p, c in zip(candles, candles[1:]):
        up, down = c["high"] - p["high"], p["low"] - c["low"]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    trs = _true_ranges(candles)
    atr_w, plus_w, minus_w = sum(trs[:period]), sum(plus_dm[:period]), sum(minus_dm[:period])
    dxs = []
    for i in range(period, len(trs)):
        atr_w = atr_w - atr_w / period + trs[i]
        plus_w = plus_w - plus_w / period + plus_dm[i]
        minus_w = minus_w - minus_w / period + minus_dm[i]
        pdi = 100 * plus_w / atr_w if atr_w else 0.0
        mdi = 100 * minus_w / atr_w if atr_w else 0.0
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
    if len(dxs) < period:
        return None
    val = sum(dxs[:period]) / period
    for d in dxs[period:]:
        val = (val * (period - 1) + d) / period
    return val


def support_resistance(candles: list[dict], lookback: int = 60, pivot: int = 2) -> dict | None:
    """Nearest swing-pivot support below and resistance above the price."""
    sl = candles[-lookback:]
    if not sl:
        return None
    lo_all, hi_all = min(c["low"] for c in sl), max(c["high"] for c in sl)
    if len(sl) < pivot * 2 + 3:
        return {"support": lo_all, "resistance": hi_all, "method": "range"}
    lows, highs = [], []
    for i in range(pivot, len(sl) - pivot):
        lo, hi = sl[i]["low"], sl[i]["high"]
        is_low = all(not (sl[i - j]["low"] <= lo or sl[i + j]["low"] < lo) for j in range(1, pivot + 1))
        is_high = all(not (sl[i - j]["high"] >= hi or sl[i + j]["high"] > hi) for j in range(1, pivot + 1))
        if is_low:
            lows.append(lo)
        if is_high:
            highs.append(hi)
    price = sl[-1]["close"]
    supports = sorted((v for v in lows if v < price), reverse=True)
    resistances = sorted(v for v in highs if v > price)
    return {"support": supports[0] if supports else lo_all,
            "resistance": resistances[0] if resistances else hi_all, "method": "swing"}


def detect_regime(ind: dict) -> dict:
    """trending | ranging, used for the signal's regime weights."""
    adx_v, atr_v, price = ind.get("adx14"), ind.get("atr14"), ind.get("price")
    atr_pct = atr_v / price * 100 if atr_v is not None and price else None
    if adx_v is not None and adx_v >= 25:
        return {"regime": "trending", "adx": adx_v, "atr_pct": atr_pct, "reason": "ADX ≥ 25", "reason_ar": "مؤشر الاتجاه ≥ 25"}
    if adx_v is not None and adx_v <= 18:
        return {"regime": "ranging", "adx": adx_v, "atr_pct": atr_pct, "reason": "ADX ≤ 18", "reason_ar": "مؤشر الاتجاه ≤ 18"}
    if atr_pct is not None and atr_pct > 1.5:
        return {"regime": "trending", "adx": adx_v, "atr_pct": atr_pct, "reason": "elevated ATR%", "reason_ar": "تذبذب مرتفع (ATR%)"}
    return {"regime": "ranging", "adx": adx_v, "atr_pct": atr_pct, "reason": "low directional strength", "reason_ar": "ضعف الاتجاه"}


def compute(candles: list[dict], with_series: bool = True) -> dict:
    closes = [c["close"] for c in candles]
    out = {
        "price": closes[-1] if closes else None,
        "sma20": sma(closes, 20), "sma50": sma(closes, 50),
        "ema20": ema(closes, 20), "ema50": ema(closes, 50),
        "rsi14": rsi(closes, 14), "macd": macd(closes), "bollinger": bollinger(closes, 20, 2),
        "atr14": atr(candles, 14), "adx14": adx(candles, 14),
        "levels": support_resistance(candles, 60, 2),
    }
    if with_series:
        out["series"] = {"close": closes, "sma20": sma_series(closes, 20), "ema20": ema_series(closes, 20),
                         "bollinger": bollinger_series(closes, 20, 2), "times": [c["time"] for c in candles]}
    return out
