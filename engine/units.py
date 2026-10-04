"""Pip, price and volume conversions.

Convention from the spec: 10 pips = $1 of gold price, so 1 pip = 0.10.
Broker "points" are never used, because brokers quote gold with 2 or 3 decimals.
"""
import math

PIP = 0.10  # price distance of one pip
_EPS = 1e-9


def pips_to_price(pips: float, tick_size: float) -> float:
    """Convert a pip distance to a price distance rounded to the symbol's tick size."""
    return round_to_tick(pips * PIP, tick_size)


def price_to_pips(distance: float) -> float:
    return round(distance / PIP, 1)


def round_to_tick(price: float, tick_size: float) -> float:
    ticks = round(price / tick_size)
    return round(ticks * tick_size, 10)


def floor_volume(volume: float, step: float) -> float:
    """Round a volume down to the broker's volume step."""
    return round(math.floor(volume / step + _EPS) * step, 8)


def is_volume_multiple(volume: float, step: float) -> bool:
    return abs(volume / step - round(volume / step)) < 1e-6


def split_volumes(lot: float, close_pcts: list[float], vol_min: float, vol_step: float) -> list[float]:
    """Volume to close at each step, as a share of the original lot.

    Each share is rounded down to the volume step. A share below the broker's
    minimum lot becomes 0 and stays on the runner. The total never exceeds the lot.
    """
    volumes = []
    remaining = lot
    for pct in close_pcts:
        v = floor_volume(lot * pct / 100.0, vol_step)
        if v < vol_min - _EPS:
            v = 0.0
        v = min(v, floor_volume(remaining, vol_step))
        remaining = round(remaining - v, 8)
        volumes.append(v)
    return volumes


def dollars(pips: float, volume: float, contract_size: float = 100.0) -> float:
    """Dollar value of a pip move for a volume. 1.00 lot = 100 oz on most brokers."""
    return round(pips * PIP * volume * contract_size, 2)
