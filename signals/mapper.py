"""Turn a parsed signal + channel trust + our preset rules into a concrete trade plan."""
from __future__ import annotations

from dataclasses import replace

from engine.broker import Quote, SymbolInfo
from engine.models import STOP_MODE_PIPS, Side, StepConfig, StopMode, TradeSettings
from engine.units import PIP, floor_volume

from .models import MAX_WEIGHT, Channel, ParsedSignal, SignalSettings


def lot_for(channel: Channel, cfg: SignalSettings, info: SymbolInfo) -> float:
    """Lot by trust: weight x lot_per_weight (weight 1 -> 0.01, weight 5 -> 0.05 by default)."""
    raw = cfg.lot_per_weight * max(1, min(channel.weight, MAX_WEIGHT))
    return max(info.vol_min, floor_volume(raw, info.vol_step))


def build_plan(p: ParsedSignal, channel: Channel, preset: TradeSettings, cfg: SignalSettings,
               quote: Quote, info: SymbolInfo) -> dict:
    """Returns {side, settings(dict), lot, ref_price, errors[], warnings[]} for an "open" signal."""
    errors: list[str] = []
    warnings: list[str] = []
    side = Side(p.side)
    sign = side.sign
    ref = quote.ask if side is Side.BUY else quote.bid      # we enter at market

    if p.entry is not None:
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

    lot = lot_for(channel, cfg, info)
    settings = TradeSettings(lot=lot, stop_mode=StopMode.FAR, sl_pips=sl_pips, tp_pips=tp_pips,
                             steps=steps, trailing=preset.trailing, trail_pips=preset.trail_pips)
    return {
        "side": side.value, "lot": lot, "ref_price": ref, "sl_price": p.sl, "tps": p.tps,
        "settings": _settings_dict(settings), "errors": errors, "warnings": warnings,
        "weight": channel.weight, "preset_steps_used": steps is preset.steps,
    }


def _settings_dict(s: TradeSettings) -> dict:
    return {"lot": s.lot, "stop_mode": s.stop_mode.value, "sl_pips": s.sl_pips, "tp_pips": s.tp_pips,
            "steps": [{"pips": st.pips, "close_pct": st.close_pct, "move_sl_pips": st.move_sl_pips} for st in s.steps],
            "trailing": s.trailing, "trail_pips": s.trail_pips}


def settings_from_dict(d: dict) -> TradeSettings:
    return TradeSettings(lot=d["lot"], stop_mode=StopMode(d["stop_mode"]), sl_pips=d.get("sl_pips"),
                         tp_pips=d.get("tp_pips"),
                         steps=[StepConfig(s["pips"], s.get("close_pct", 0), s.get("move_sl_pips")) for s in d["steps"]],
                         trailing=d.get("trailing", True), trail_pips=d.get("trail_pips", 50))
