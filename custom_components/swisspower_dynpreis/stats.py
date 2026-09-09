"""Derived statistics over the cached price curve.

These are pure functions of (slots, instant). They live outside the platform
modules because the sensor and the binary sensor platform both need them, and
because they are then testable without a Home Assistant instance.

Every function takes the render instant explicitly. One render must not mix two
different clock readings - which is exactly what would happen at the slot and
midnight boundaries the coordinator's render timer deliberately fires on.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .pricing import (
    average_price_for_window,
    day_bounds,
    find_current_slot,
    normalize_price_slots,
    parse_timestamp,
    percentile_threshold,
    window_extreme,
)


def next_change(
    slots: list[dict[str, Any]],
    now: datetime,
    tariff_type: str,
    component: str | None,
) -> datetime | None:
    """Return when the current price stops being current."""
    slot = find_current_slot(slots, now)
    if not slot:
        return None
    return parse_timestamp(slot.get("end_timestamp"))


def average_for_day(
    slots: list[dict[str, Any]],
    now: datetime,
    offset_days: int,
    tariff_type: str,
    component: str | None,
) -> float | None:
    """Return the time-weighted average price for a local day."""
    normalized = normalize_price_slots(slots, tariff_type, component)
    start, end_exclusive = day_bounds(now, offset_days)
    return average_price_for_window(normalized, start, end_exclusive)


def day_stats(
    slots: list[dict[str, Any]],
    now: datetime,
    offset_days: int,
    tariff_type: str,
    component: str | None,
) -> dict[str, Any]:
    """Return min/max/average statistics for a local day."""
    normalized = normalize_price_slots(slots, tariff_type, component)
    start, end_exclusive = day_bounds(now, offset_days)
    values = [slot.value for slot in normalized if start <= slot.start < end_exclusive]
    if not values:
        return {}
    return {
        "min_price": min(values),
        "max_price": max(values),
        "average_price": sum(values) / len(values),
        "slots": len(values),
    }


def window_value(
    slots: list[dict[str, Any]],
    now: datetime,
    offset_days: int,
    window_hours: int,
    tariff_type: str,
    component: str | None,
    extreme: str,
) -> float | None:
    """Return the average price of the cheapest or dearest window of a day."""
    normalized = normalize_price_slots(slots, tariff_type, component)
    start, end_exclusive = day_bounds(now, offset_days)
    result = window_extreme(
        normalized, start, end_exclusive, window_hours, extreme=extreme
    )
    if not result:
        return None
    return result[0]


def window_attrs(
    slots: list[dict[str, Any]],
    now: datetime,
    offset_days: int,
    window_hours: int,
    tariff_type: str,
    component: str | None,
    extreme: str,
) -> dict[str, Any]:
    """Return where the cheapest or dearest window of a day sits."""
    normalized = normalize_price_slots(slots, tariff_type, component)
    start, end_exclusive = day_bounds(now, offset_days)
    result = window_extreme(
        normalized, start, end_exclusive, window_hours, extreme=extreme
    )
    if not result:
        return {}
    average, window_start, window_end = result
    return {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_average": average,
    }


def percentile_binary(
    slots: list[dict[str, Any]],
    now: datetime,
    percentile: float,
    tariff_type: str,
    component: str | None,
    highest: bool,
) -> bool | None:
    """Return whether the current price is in the cheapest/dearest share of today.

    None means "no price for this instant" and must stay None: reporting it as
    False would claim the current price is outside the cheap band when in fact
    nothing is known about it.
    """
    normalized = normalize_price_slots(slots, tariff_type, component)
    start, end_exclusive = day_bounds(now, 0)
    day_slots = [slot for slot in normalized if start <= slot.start < end_exclusive]
    if not day_slots:
        return None
    current = next((slot for slot in day_slots if slot.start <= now <= slot.end), None)
    if not current:
        return None
    threshold = percentile_threshold(
        [slot.value for slot in day_slots], percentile, highest=highest
    )
    if threshold is None:
        return None
    if highest:
        return current.value >= threshold
    return current.value <= threshold


def in_window(
    slots: list[dict[str, Any]],
    now: datetime,
    offset_days: int,
    window_hours: int,
    tariff_type: str,
    component: str | None,
    extreme: str,
) -> bool | None:
    """Return whether the instant falls inside a day's cheapest/dearest window."""
    normalized = normalize_price_slots(slots, tariff_type, component)
    start, end_exclusive = day_bounds(now, offset_days)
    result = window_extreme(
        normalized, start, end_exclusive, window_hours, extreme=extreme
    )
    if not result:
        return None
    _, window_start, window_end = result
    return window_start <= now <= window_end
