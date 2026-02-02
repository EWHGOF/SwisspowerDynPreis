"""Pricing helpers for Swisspower DynPreis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util


@dataclass(frozen=True)
class PriceSlot:
    """Normalized price slot."""

    start: datetime
    end: datetime
    value: float


def extract_slot_value(
    slot: dict[str, Any],
    tariff_type: str,
    component: str | None,
) -> float | None:
    """Extract the slot value for a given tariff type and component."""
    if component is None and isinstance(slot.get("value"), (int, float)):
        return slot.get("value")
    prices = slot.get(tariff_type)
    if isinstance(prices, list):
        for price in prices:
            if not isinstance(price, dict):
                continue
            if component is not None and price.get("component") != component:
                continue
            if price.get("unit") == "CHF/kWh" and price.get("value") is not None:
                return price.get("value")
            if price.get("value") is not None:
                return price.get("value")
    if slot.get("unit") == "CHF/kWh":
        if component is None and slot.get("component") in (None, "work"):
            return slot.get("value")
        if component is not None and slot.get("component") == component:
            return slot.get("value")
    return None


def find_current_slot(slots: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """Find the current slot for a given time."""
    for slot in slots:
        start = dt_util.parse_datetime(slot.get("start_timestamp"))
        end = dt_util.parse_datetime(slot.get("end_timestamp"))
        if not start or not end:
            continue
        if start <= now <= end:
            return slot
    return None


def normalize_price_slots(
    slots: list[dict[str, Any]],
    tariff_type: str,
    component: str | None = None,
) -> list[PriceSlot]:
    """Normalize raw slots into typed price slots."""
    normalized: list[PriceSlot] = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        start = dt_util.parse_datetime(slot.get("start_timestamp"))
        end = dt_util.parse_datetime(slot.get("end_timestamp"))
        if not start or not end:
            continue
        value = extract_slot_value(slot, tariff_type, component)
        if value is None:
            continue
        normalized.append(PriceSlot(start=start, end=end, value=value))
    return sorted(normalized, key=lambda item: item.start)


def day_bounds(offset_days: int) -> tuple[datetime, datetime]:
    """Return start and end-exclusive datetimes for the local day offset."""
    start = dt_util.start_of_local_day(dt_util.now()) + timedelta(days=offset_days)
    end_exclusive = start + timedelta(days=1)
    return start, end_exclusive


def average_price_for_window(
    slots: list[PriceSlot],
    start: datetime,
    end_exclusive: datetime,
) -> float | None:
    """Calculate the weighted average price for a time window."""
    total_seconds = 0.0
    weighted_sum = 0.0
    for slot in slots:
        slot_start = max(slot.start, start)
        slot_end_excl = min(slot.end + timedelta(seconds=1), end_exclusive)
        if slot_start >= slot_end_excl:
            continue
        seconds = (slot_end_excl - slot_start).total_seconds()
        weighted_sum += slot.value * seconds
        total_seconds += seconds
    if total_seconds == 0:
        return None
    return weighted_sum / total_seconds


def window_extreme(
    slots: list[PriceSlot],
    start: datetime,
    end_exclusive: datetime,
    window_hours: int,
    *,
    extreme: str,
) -> tuple[float, datetime, datetime] | None:
    """Find the cheapest/most expensive consecutive window."""
    if window_hours <= 0:
        return None
    day_slots = [slot for slot in slots if start <= slot.start < end_exclusive]
    if not day_slots:
        return None
    best: tuple[float, datetime, datetime] | None = None
    for index in range(len(day_slots) - window_hours + 1):
        window = day_slots[index : index + window_hours]
        if len(window) < window_hours:
            continue
        contiguous = True
        for prev, next_slot in zip(window, window[1:]):
            if prev.end + timedelta(seconds=1) != next_slot.start:
                contiguous = False
                break
        if not contiguous:
            continue
        avg = sum(slot.value for slot in window) / window_hours
        window_start = window[0].start
        window_end = window[-1].end
        if best is None:
            best = (avg, window_start, window_end)
            continue
        if extreme == "min" and avg < best[0]:
            best = (avg, window_start, window_end)
        if extreme == "max" and avg > best[0]:
            best = (avg, window_start, window_end)
    return best


def percentile_threshold(values: list[float], percentile: float, *, highest: bool) -> float | None:
    """Return threshold value for a percentile slice."""
    if not values:
        return None
    if percentile <= 0:
        return None
    count = max(1, int(round(len(values) * percentile)))
    values_sorted = sorted(values)
    if highest:
        return values_sorted[-count]
    return values_sorted[count - 1]
