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
    PriceSlot,
    average_price_for_window,
    coverage_seconds,
    day_bounds,
    elapsed,
    find_current_slot,
    normalize_price_slots,
    parse_timestamp,
    percentile_threshold,
    slot_payload,
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


def slots_for_day(
    slots: list[PriceSlot],
    now: datetime,
    offset_days: int,
) -> list[dict[str, Any]]:
    """Return one local day's slots in the flat, chart-ready form.

    Unlike the functions above this takes already normalized slots. The three
    display helpers are called together, once per state write, and normalizing
    inside each of them would walk the whole curve three times over instead of
    once - up to 96 times a day with quarter-hourly prices.
    """
    start, end_exclusive = day_bounds(now, offset_days)
    return [slot_payload(slot) for slot in slots if start <= slot.start < end_exclusive]


def upcoming_slots(
    slots: list[PriceSlot],
    now: datetime,
) -> list[dict[str, Any]]:
    """Return every slot that has not ended yet, in the flat form.

    One attribute for "what will the tariff be", however many days ahead the
    fetch window happens to reach - so widening the window does not need a
    further attribute per day. The slot covering ``now`` is included: it is the
    price still in force, not a past one.
    """
    return [slot_payload(slot) for slot in slots if slot.end >= now]


def day_summaries(
    slots: list[PriceSlot],
    now: datetime,
    days: int,
) -> list[dict[str, Any]]:
    """Summarize each local day the fetch window reaches.

    ``coverage`` is the share of the day's real length that carries a price, so
    a half-published day is distinguishable from a complete one without a flag
    of its own - which matters most for the last day in the window, where a
    supplier that publishes day-ahead only will leave everything empty.

    ``average`` is weighted by how long each slot lasts and clips to the day, so
    a slot straddling midnight counts only for its part. ``min``/``max`` look at
    the slots that start in the day, matching day_stats above.
    """
    summaries: list[dict[str, Any]] = []
    for offset in range(days):
        start, end_exclusive = day_bounds(now, offset)
        span = elapsed(start, end_exclusive).total_seconds()
        values = [
            slot.value for slot in slots if start <= slot.start < end_exclusive
        ]
        average = average_price_for_window(slots, start, end_exclusive)
        summaries.append(
            {
                # day_bounds returns local instants, so this is the local date.
                "date": start.date().isoformat(),
                # Rounded because this is a display attribute: a weighted mean
                # of CHF/kWh values otherwise arrives as 0.14750000000000002.
                # Six decimals is two more than the API publishes.
                "average": None if average is None else round(average, 6),
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "slots": len(values),
                "coverage": (
                    round(coverage_seconds(slots, start, end_exclusive) / span, 3)
                    if span > 0
                    else 0.0
                ),
            }
        )
    return summaries


def day_is_complete(
    slots: list[PriceSlot],
    now: datetime,
    offset_days: int,
    ratio: float,
) -> bool:
    """Return whether a local day is published for these slots.

    The coordinator has its own tomorrow_complete, but that one asks about
    *every* configured tariff type at once, because it drives the fetch
    schedule: as long as one type is missing tomorrow, there is a reason to ask
    again. As a per-entity attribute the same answer would be wrong - an
    electricity sensor with a full curve for tomorrow would report "not
    published" only because the grid tariff has not been published yet.

    This one asks about the slots handed in, so about one tariff type and one
    component, and counts only slots a value can be read from - a slot with no
    price for this component is nothing a dashboard can draw.
    """
    start, end_exclusive = day_bounds(now, offset_days)
    span = elapsed(start, end_exclusive).total_seconds()
    if span <= 0:
        return False
    return coverage_seconds(slots, start, end_exclusive) >= span * ratio


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
