"""Pricing helpers for Swisspower DynPreis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util


# Slot ends are stored inclusively (the last second that still belongs to the
# slot), so a slot's duration is end - start plus one second.
SECOND = timedelta(seconds=1)


@dataclass(frozen=True)
class PriceSlot:
    """Normalized price slot."""

    start: datetime
    end: datetime
    value: float

    @property
    def duration(self) -> timedelta:
        """Return how long the slot really lasts."""
        return elapsed(self.start, self.end) + SECOND


def slot_payload(slot: PriceSlot) -> dict[str, Any]:
    """Return the flat, chart-ready form of one slot.

    The raw API shape nests the value under the tariff type and a component
    list, so anything that wants to draw the curve has to reimplement
    extract_slot_value. This is the shape that does not: three keys, the value
    already resolved. ``end`` stays inclusive - the last second that still
    belongs to the slot - the way every other timestamp here is.
    """
    return {
        "start": slot.start.isoformat(),
        "end": slot.end.isoformat(),
        "value": slot.value,
    }


def elapsed(earlier: datetime, later: datetime) -> timedelta:
    """Return the real time between two instants.

    Subtracting two aware datetimes that share one tzinfo object ignores the
    offset - CPython treats them as naive - which silently turns the 23- and
    25-hour DST days into 24 hours. Converting to UTC first makes the naive
    subtraction correct by construction.
    """
    return dt_util.as_utc(later) - dt_util.as_utc(earlier)


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


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a slot timestamp, tolerating a slot that carries none.

    dt_util.parse_datetime raises TypeError for anything that is not a string,
    so a slot the API sent without a usable timestamp would otherwise take down
    every render that walks the price list.
    """
    if not isinstance(value, str):
        return None
    return dt_util.parse_datetime(value)


def slot_bounds(slot: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """Return the start/end of a slot, or None if either is unusable."""
    start = parse_timestamp(slot.get("start_timestamp"))
    end = parse_timestamp(slot.get("end_timestamp"))
    if start is None or end is None:
        return None
    return start, end


def find_current_slot(slots: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """Find the current slot for a given time."""
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        bounds = slot_bounds(slot)
        if bounds is None:
            continue
        start, end = bounds
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
        bounds = slot_bounds(slot)
        if bounds is None:
            continue
        start, end = bounds
        value = extract_slot_value(slot, tariff_type, component)
        if value is None:
            continue
        normalized.append(PriceSlot(start=start, end=end, value=value))
    return sorted(normalized, key=lambda item: item.start)


def day_bounds(now: datetime, offset_days: int) -> tuple[datetime, datetime]:
    """Return start and end-exclusive datetimes for the local day offset.

    ``now`` is passed in rather than read from the clock so that one render
    cannot mix two different instants - which is exactly what happens at the
    slot and midnight boundaries the render timer deliberately fires on.
    """
    local_now = dt_util.as_local(now)
    start = dt_util.start_of_local_day(local_now + timedelta(days=offset_days))
    end_exclusive = dt_util.start_of_local_day(
        local_now + timedelta(days=offset_days + 1)
    )
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
        seconds = elapsed(slot_start, slot_end_excl).total_seconds()
        weighted_sum += slot.value * seconds
        total_seconds += seconds
    if total_seconds == 0:
        return None
    return weighted_sum / total_seconds


def coverage_seconds(
    slots: list[PriceSlot],
    start: datetime,
    end_exclusive: datetime,
) -> float:
    """Return how many seconds of a window actually carry a price.

    average_price_for_window computes this total on its way to the mean but
    only returns the mean. Kept as its own function rather than handed back as
    a second value, so the callers that just want an average are not made to
    unpack a tuple.

    Counting seconds rather than slots is what makes the answer hold for any
    slot length and for the 23- and 25-hour days around a DST change.
    """
    covered = 0.0
    for slot in slots:
        slot_start = max(slot.start, start)
        slot_end_excl = min(slot.end + SECOND, end_exclusive)
        if slot_start >= slot_end_excl:
            continue
        covered += elapsed(slot_start, slot_end_excl).total_seconds()
    return covered


def window_extreme(
    slots: list[PriceSlot],
    start: datetime,
    end_exclusive: datetime,
    window_hours: int,
    *,
    extreme: str,
) -> tuple[float, datetime, datetime] | None:
    """Find the cheapest or most expensive contiguous window in the day.

    ``window_hours`` is a real duration, not a number of slots: a two-hour
    window is eight 15-minute slots or two hourly ones. The average is weighted
    by how long each slot contributes, and a slot that reaches past the end of
    the window only counts for the part inside it - so the answer does not
    depend on how finely the supplier happens to divide the day.

    Returns (average, window start, inclusive window end), or None when no
    contiguous run in the day is long enough.
    """
    if window_hours <= 0:
        return None
    target = timedelta(hours=window_hours)
    day_slots = sorted(
        (slot for slot in slots if start <= slot.start < end_exclusive),
        key=lambda slot: slot.start,
    )

    best: tuple[float, datetime, datetime] | None = None
    for index, first in enumerate(day_slots):
        weighted = 0.0
        covered = timedelta()
        window_end: datetime | None = None
        previous: PriceSlot | None = None

        for slot in day_slots[index:]:
            if previous is not None and elapsed(previous.end, slot.start) != SECOND:
                # A gap in the curve: this run cannot be extended.
                break
            remaining = target - covered
            duration = slot.duration
            if duration >= remaining:
                weighted += slot.value * remaining.total_seconds()
                covered = target
                window_end = slot.start + remaining - SECOND
                break
            weighted += slot.value * duration.total_seconds()
            covered += duration
            window_end = slot.end
            previous = slot

        if covered < target or window_end is None:
            continue

        average = weighted / target.total_seconds()
        candidate = (average, first.start, window_end)
        if (
            best is None
            or (extreme == "min" and average < best[0])
            or (extreme == "max" and average > best[0])
        ):
            best = candidate

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
