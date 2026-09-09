"""Unit tests for the pricing maths. No Home Assistant instance needed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.swisspower_dynpreis.pricing import (
    SECOND,
    PriceSlot,
    average_price_for_window,
    coverage_seconds,
    elapsed,
    slot_payload,
    window_extreme,
)

# A fixed offset, which is what parsing an ISO timestamp yields.
OFFSET = timezone(timedelta(hours=2))


def slots(values: list[float], *, slot_minutes: int) -> list[PriceSlot]:
    """Build contiguous slots of the given length starting at 00:00."""
    step = timedelta(minutes=slot_minutes)
    cursor = datetime(2026, 9, 7, 0, 0, tzinfo=OFFSET)
    built: list[PriceSlot] = []
    for value in values:
        built.append(
            PriceSlot(start=cursor, end=cursor + step - SECOND, value=value)
        )
        cursor += step
    return built


def day() -> tuple[datetime, datetime]:
    start = datetime(2026, 9, 7, 0, 0, tzinfo=OFFSET)
    return start, start + timedelta(days=1)


def test_slot_duration_counts_the_inclusive_end() -> None:
    hourly = slots([0.1], slot_minutes=60)
    assert hourly[0].duration == timedelta(hours=1)
    quarterly = slots([0.1], slot_minutes=15)
    assert quarterly[0].duration == timedelta(minutes=15)


def test_elapsed_survives_a_dst_day() -> None:
    """Real elapsed time, not wall-clock arithmetic."""
    from homeassistant.util import dt as dt_util

    start = dt_util.parse_datetime("2026-10-25T00:00:00+02:00")
    end = dt_util.parse_datetime("2026-10-26T00:00:00+01:00")
    assert start is not None and end is not None
    assert elapsed(start, end) == timedelta(hours=25)


def test_two_hour_window_is_two_hours_of_hourly_slots() -> None:
    """The cheapest two hours of a day of hourly prices."""
    values = [0.5] * 24
    values[8] = 0.1
    values[9] = 0.1
    start, end = day()

    result = window_extreme(slots(values, slot_minutes=60), start, end, 2, extreme="min")

    assert result is not None
    average, window_start, window_end = result
    assert average == pytest.approx(0.1)
    assert window_start.hour == 8
    # Inclusive end: the last second of the 09:00 slot.
    assert elapsed(window_start, window_end) == timedelta(hours=2) - SECOND


def test_two_hour_window_is_two_hours_of_quarter_hourly_slots() -> None:
    """Regression: the window used to be a slot count, so this was 30 minutes.

    Eight cheap quarter-hours in a row are exactly two hours. Only four cheap
    ones must not win, even though four slots would have counted as "2h" under
    the old slot-counting implementation.
    """
    values = [0.5] * 96
    # Four very cheap quarter-hours at 02:00 - one hour in total.
    for index in range(8, 12):
        values[index] = 0.0
    # Eight moderately cheap ones at 05:00 - two full hours.
    for index in range(20, 28):
        values[index] = 0.2
    start, end = day()

    result = window_extreme(slots(values, slot_minutes=15), start, end, 2, extreme="min")

    assert result is not None
    average, window_start, window_end = result
    assert elapsed(window_start, window_end) == timedelta(hours=2) - SECOND
    # The genuine two-hour stretch wins: 0.2 throughout.
    assert average == pytest.approx(0.2)
    assert window_start.hour == 5
    assert window_start.minute == 0


def test_window_average_is_weighted_by_duration() -> None:
    """A window that ends inside a slot only counts the part it covers."""
    # 30-minute slots: 0.0, 0.0, 0.0, then 1.0 for the rest.
    values = [0.0, 0.0, 0.0] + [1.0] * 45
    start, end = day()

    result = window_extreme(slots(values, slot_minutes=30), start, end, 2, extreme="min")

    assert result is not None
    average, _, _ = result
    # 90 minutes at 0.0 plus 30 minutes at 1.0, over two hours.
    assert average == pytest.approx(0.25)


def test_no_window_when_the_day_is_too_short() -> None:
    """A day with only one hour of data has no two-hour window."""
    start, end = day()
    assert window_extreme(slots([0.1], slot_minutes=60), start, end, 2, extreme="min") is None


def test_a_gap_breaks_the_window() -> None:
    """A missing hour must not be bridged."""
    start, end = day()
    built = slots([0.1] * 4, slot_minutes=60)
    # Drop the 01:00 slot, leaving 00:00 then 02:00-03:00.
    with_gap = [built[0], built[2], built[3]]

    result = window_extreme(with_gap, start, end, 2, extreme="min")

    assert result is not None
    # Only the 02:00-03:00 pair is contiguous.
    assert result[1].hour == 2


def test_most_expensive_window_picks_the_maximum() -> None:
    values = [0.1] * 24
    values[18] = 0.9
    values[19] = 0.9
    start, end = day()

    result = window_extreme(slots(values, slot_minutes=60), start, end, 2, extreme="max")

    assert result is not None
    average, window_start, _ = result
    assert average == pytest.approx(0.9)
    assert window_start.hour == 18


def test_average_price_for_window_weights_by_real_time() -> None:
    """Uneven slot lengths must not be averaged as if they were equal."""
    base = datetime(2026, 9, 7, 0, 0, tzinfo=OFFSET)
    uneven = [
        # Three hours at 1.0 ...
        PriceSlot(start=base, end=base + timedelta(hours=3) - SECOND, value=1.0),
        # ... then one hour at 5.0.
        PriceSlot(
            start=base + timedelta(hours=3),
            end=base + timedelta(hours=4) - SECOND,
            value=5.0,
        ),
    ]

    average = average_price_for_window(uneven, base, base + timedelta(hours=4))

    # (3 * 1.0 + 1 * 5.0) / 4, not the unweighted (1.0 + 5.0) / 2.
    assert average == pytest.approx(2.0)


def test_coverage_counts_only_the_part_inside_the_window() -> None:
    """A slot reaching past the window edge counts for its overlap alone."""
    start, end_exclusive = day()
    # Six hourly slots from 22:00 the previous day, so two of them are inside.
    cursor = start - timedelta(hours=2)
    built: list[PriceSlot] = []
    for _ in range(6):
        built.append(PriceSlot(start=cursor, end=cursor + timedelta(hours=1) - SECOND, value=0.1))
        cursor += timedelta(hours=1)
    assert coverage_seconds(built, start, end_exclusive) == 4 * 3600


def test_coverage_of_a_full_day_of_quarter_hours() -> None:
    """96 quarter-hour slots cover the day exactly, with no gap or overlap."""
    start, end_exclusive = day()
    assert coverage_seconds(slots([0.1] * 96, slot_minutes=15), start, end_exclusive) == (
        24 * 3600
    )


def test_coverage_measures_the_real_length_of_a_dst_day() -> None:
    """The 25-hour day needs 25 hours of prices, not 24.

    A slot count cannot express this, which is why coverage is in seconds: 24
    hourly slots on this day leave an hour uncovered, and a ratio built on a
    24-hour assumption would call that day complete.
    """
    from homeassistant.util import dt as dt_util

    start = dt_util.parse_datetime("2026-10-25T00:00:00+02:00")
    end_exclusive = dt_util.parse_datetime("2026-10-26T00:00:00+01:00")
    assert start is not None and end_exclusive is not None

    cursor = start
    built: list[PriceSlot] = []
    for _ in range(25):
        nxt = dt_util.as_local(dt_util.as_utc(cursor) + timedelta(hours=1))
        built.append(PriceSlot(start=cursor, end=nxt - SECOND, value=0.1))
        cursor = nxt

    span = elapsed(start, end_exclusive).total_seconds()
    assert span == 25 * 3600
    assert coverage_seconds(built, start, end_exclusive) == span
    # 24 of the 25 slots leave exactly one hour of the day without a price.
    assert coverage_seconds(built[:24], start, end_exclusive) == 24 * 3600


def test_coverage_ignores_a_slot_outside_the_window() -> None:
    start, end_exclusive = day()
    far = [PriceSlot(start=start + timedelta(days=3), end=start + timedelta(days=3, hours=1), value=0.1)]
    assert coverage_seconds(far, start, end_exclusive) == 0.0


def test_slot_payload_keeps_the_inclusive_end() -> None:
    """The flat form is three keys, and the end is still the last second."""
    slot = slots([0.1234], slot_minutes=15)[0]
    assert slot_payload(slot) == {
        "start": "2026-09-07T00:00:00+02:00",
        "end": "2026-09-07T00:14:59+02:00",
        "value": 0.1234,
    }
