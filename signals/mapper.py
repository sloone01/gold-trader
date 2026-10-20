"""Turn a parsed signal + channel trust + our preset rules into a concrete trade plan."""
from __future__ import annotations

from dataclasses import replace

from engine.broker import Quote, SymbolInfo
from engine.models import STOP_MODE_PIPS, Side, StepConfig, StopMode, TradeSettings
from engine.units import PIP, dollars, floor_volume

from .models import MAX_WEIGHT, Channel, ParsedSignal, SignalSettings


def lot_for(channel: Channel, cfg: SignalSettings, info: SymbolInfo, sl_pips: float | None = None,
            balance: float | None = None) -> tuple[float, float | None]:
    """(lot, $ at risk). By trust weight: weight x lot_per_weight. By risk: weight x risk_pct_per_weight %
    of the balance, divided by what the stop distance costs per lot."""
    w = max(1, min(channel.weight, MAX_WEIGHT))
    if cfg.sizing == "risk" and balance and sl_pips and sl_pips > 0:
        money = balance * w * cfg.risk_pct_per_weight / 100
        per_lot = dollars(sl_pips, 1.0, info.contract_size)
        return max(info.vol_min, floor_volume(money / per_lot, info.vol_step)), round(money, 2)
    lot = max(info.vol_min, floor_volume(cfg.lot_per_weight * w, info.vol_step))
    return lot, (dollars(sl_pips, lot, info.contract_size) if sl_pips else None)


def plan_legs(p: ParsedSignal, cfg: SignalSettings, lot: float, info: SymbolInfo) -> list[dict]:
    """Entry legs: [{price (None = now), kind: market|limit|stop, lot}]. One leg unless the ladder is on
    or the signal is a pending order."""
    side = Side(p.side)
    if p.order_type == "stop":                     # breakout: one entry once price gets there
        return [{"price": p.entry, "kind": "stop", "lot": lot}]
    n = max(1, min(int(cfg.ladder_legs), 3)) if cfg.ladder else 1
    while n > 1 and floor_volume(lot / n, info.vol_step) < info.vol_min:
        n -= 1                                     # too small to split
    if p.entry is not None and p.entry_high is not None and p.entry_high != p.entry:
        top, bottom = (p.entry_high, p.entry) if side is Side.BUY else (p.entry, p.entry_high)
        prices = [top, (top + bottom) / 2, bottom][:n] if n == 3 else [top, bottom][:n] if n == 2 else [top]
        kinds = ["limit"] * n                      # zone: each edge fills when price comes to it
        if p.order_type == "market":
            kinds[0] = "market"                    # first leg now (if price is at or better than the zone)
    else:
        first = p.entry if p.order_type == "limit" else None
        prices, kinds = [first], ["limit" if first is not None else "market"]
        if n > 1 and p.sl is not None:
            ref = p.entry if p.entry is not None else None
            if ref is not None:
                depth = cfg.ladder_depth_pct / 100
                for k in range(1, n):
                    prices.append(round(ref - (ref - p.sl) * depth * k / (n - 1), 2))
                    kinds.append("limit")
    each = floor_volume(lot / len(prices), info.vol_step)
    lots = [each] * len(prices)
    lots[0] = round(lots[0] + floor_volume(lot - each * len(prices), info.vol_step), 8)   # leftovers on the first
    # A market leg with a price (a zone's first edge) fills now if the market is at or better than it.
    return [{"price": round(pr, 2) if pr is not None else None, "kind": k, "lot": l}
            for pr, k, l in zip(prices, kinds, lots)]


def build_plan(p: ParsedSignal, channel: Channel, preset: TradeSettings, cfg: SignalSettings,
               quote: Quote, info: SymbolInfo, lot: float | None = None, balance: float | None = None,
               max_lot: float | None = None, entry_at: float | None = None) -> dict:
    """Returns {side, settings(dict), lot, ref_price, errors[], warnings[]} for an "open" signal.
    lot: a fixed lot (one ladder leg); entry_at: plan as if entering at this price (a waiting leg)."""
    errors: list[str] = []
    warnings: list[str] = []
    side = Side(p.side)
    sign = side.sign
    ref = entry_at if entry_at is not None else (quote.ask if side is Side.BUY else quote.bid)

    if p.entry is not None and entry_at is None and p.order_type == "market":
        lo, hi = p.entry, p.entry_high or p.entry
        if lo <= ref <= hi:
            drift = 0.0
        else:
            drift = min(abs(ref - lo), abs(ref - hi)) / PIP
        if drift > cfg.max_entry_drift_pips:
            warnings.append(f"Market is {drift:.0f} pips from the signal entry {lo:g}" + (f"-{hi:g}" if hi != lo else ""))

    if p.sl is None:
        # No stop in the message: enter now with the default stop; a later "stop X" message moves it.
        sl_pips = cfg.default_sl_pips
        warnings.append(f"No stop loss in the signal; using the default {sl_pips:g} pips until the channel gives one")
    else:
        sl_pips = (ref - p.sl) * sign / PIP
        if sl_pips <= 0:
            errors.append(f"Stop loss {p.sl:g} is on the wrong side of the price for a {side.value}")
        sl_pips = round(sl_pips, 1)

    tp_dists = [round((tp - ref) * sign / PIP, 1) for tp in p.tps]
    good_tps = [d for d in tp_dists if d > 0]
    if tp_dists and len(good_tps) != len(tp_dists):
        warnings.append("Some TPs are already passed or on the wrong side; they were dropped")
    good_tps = sorted(set(good_tps))

    steps = list(preset.steps)
    if cfg.use_signal_tps and len(good_tps) >= 3:
        steps = [replace(st, pips=d) for st, d in zip(preset.steps, good_tps[:3])]
    elif cfg.use_signal_tps and good_tps:
        warnings.append(f"Signal has {len(good_tps)} usable TP(s); using the preset's step distances")

    tp_pips = None
    if cfg.final_tp and good_tps and good_tps[-1] > steps[2].pips:
        tp_pips = good_tps[-1]

    # Steps that lock the SL into profit must stay below their trigger (validation enforces the gap).
    fixed = []
    for st in steps:
        if st.move_sl_pips is not None and st.move_sl_pips >= st.pips:
            fixed.append(replace(st, move_sl_pips=max(0.0, round(st.pips / 2, 1))))
        else:
            fixed.append(st)
    steps = fixed

    risk_usd = None
    if lot is None:
        lot, risk_usd = lot_for(channel, cfg, info, sl_pips if sl_pips > 0 else None, balance)
        if max_lot is not None and lot > max_lot + 1e-9:
            warnings.append(f"Lot {lot:g} capped at your max lot {max_lot:g}")
            lot = max_lot
    if risk_usd is None and sl_pips > 0:
        risk_usd = dollars(sl_pips, lot, info.contract_size)
    settings = TradeSettings(lot=lot, stop_mode=StopMode.FAR, sl_pips=sl_pips, tp_pips=tp_pips,
                             steps=steps, trailing=preset.trailing, trail_pips=preset.trail_pips,
                             be_trigger_pips=preset.be_trigger_pips, be_lock_pips=preset.be_lock_pips,
                             hold_seconds=preset.hold_seconds)
    return {
        "side": side.value, "lot": lot, "ref_price": ref, "sl_price": p.sl, "tps": p.tps,
        "settings": _settings_dict(settings), "errors": errors, "warnings": warnings,
        "weight": channel.weight, "preset_steps_used": steps is preset.steps, "risk_usd": risk_usd,
        "sizing": cfg.sizing,
    }


def _settings_dict(s: TradeSettings) -> dict:
    return {"lot": s.lot, "stop_mode": s.stop_mode.value, "sl_pips": s.sl_pips, "tp_pips": s.tp_pips,
            "steps": [{"pips": st.pips, "close_pct": st.close_pct, "move_sl_pips": st.move_sl_pips} for st in s.steps],
            "trailing": s.trailing, "trail_pips": s.trail_pips, "be_trigger_pips": s.be_trigger_pips,
            "be_lock_pips": s.be_lock_pips, "hold_seconds": s.hold_seconds}


def settings_from_dict(d: dict) -> TradeSettings:
    return TradeSettings(lot=d["lot"], stop_mode=StopMode(d["stop_mode"]), sl_pips=d.get("sl_pips"),
                         tp_pips=d.get("tp_pips"),
                         steps=[StepConfig(s["pips"], s.get("close_pct", 0), s.get("move_sl_pips")) for s in d["steps"]],
                         trailing=d.get("trailing", True), trail_pips=d.get("trail_pips", 50),
                         be_trigger_pips=d.get("be_trigger_pips"), be_lock_pips=d.get("be_lock_pips", 1.0),
                         hold_seconds=d.get("hold_seconds", 0.0))
