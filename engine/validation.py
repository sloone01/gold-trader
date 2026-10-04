"""Validation rules from the spec. Returns (errors, warnings); errors block the trade."""
from __future__ import annotations

from .broker import SymbolInfo
from .models import StopMode, TradeSettings
from .units import PIP, is_volume_multiple, split_volumes


def validate(s: TradeSettings, info: SymbolInfo) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    min_stop_pips = info.stops_level / PIP

    if s.lot < info.vol_min or s.lot > info.vol_max:
        errors.append(f"Lot {s.lot} is outside the broker range {info.vol_min}-{info.vol_max}")
    if not is_volume_multiple(s.lot, info.vol_step):
        errors.append(f"Lot {s.lot} is not a multiple of the volume step {info.vol_step}")

    sl = s.effective_sl_pips()
    if sl is None:
        if s.stop_mode is not StopMode.FAR:
            errors.append("Stop loss is required")
        else:
            warnings.append("No stop loss: the backup stop will be used until you set one")
    elif sl <= 0:
        errors.append("Stop loss must be above 0 pips")
    elif sl < min_stop_pips:
        errors.append(f"Stop loss {sl} pips is closer than the broker minimum {min_stop_pips:.0f} pips")

    if len(s.steps) != 3:
        errors.append("Exactly three steps (P1, P2, P3) are required")
        return errors, warnings

    prev = 0.0
    for i, st in enumerate(s.steps, 1):
        if st.pips <= prev:
            errors.append(f"P{i} ({st.pips}) must be further than {'P%d' % (i - 1) if i > 1 else 'entry'} ({prev})")
        prev = st.pips
        if not 0 <= st.close_pct <= 100:
            errors.append(f"P{i} close % must be between 0 and 100")
        if st.move_sl_pips is not None:
            if st.move_sl_pips < 0:
                errors.append(f"P{i} can only move the stop to breakeven or into profit")
            elif st.pips - st.move_sl_pips < min_stop_pips:
                errors.append(f"P{i} moves the stop to +{st.move_sl_pips}, too close to the trigger at +{st.pips}")

    if s.tp_pips is not None:
        if s.tp_pips <= s.steps[2].pips:
            errors.append(f"TP ({s.tp_pips}) must be further than P3 ({s.steps[2].pips})")
        if s.tp_pips < min_stop_pips:
            errors.append(f"TP is closer than the broker minimum {min_stop_pips:.0f} pips")

    total = sum(st.close_pct for st in s.steps)
    if total > 100:
        errors.append(f"Close % adds up to {total}%, more than 100%")

    if s.trailing and s.trail_pips <= 0:
        errors.append("Trailing step must be above 0 pips")

    if not errors:
        vols = split_volumes(s.lot, [st.close_pct for st in s.steps], info.vol_min, info.vol_step)
        for i, (st, v) in enumerate(zip(s.steps, vols), 1):
            if st.close_pct > 0 and v == 0:
                warnings.append(f"P{i} close of {st.close_pct}% is below the minimum lot at {s.lot} lot; it stays on the runner")
            elif st.close_pct > 0 and abs(v - s.lot * st.close_pct / 100) > 1e-9:
                warnings.append(f"P{i} closes {v} lot ({st.close_pct}% rounded down to the volume step)")

    return errors, warnings
