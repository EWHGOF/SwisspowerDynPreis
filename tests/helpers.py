"""Shared test helpers for the Swisspower DynPreis integration."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.swisspower_dynpreis.binary_sensor import (
    SwisspowerDynPreisBinarySensor,
)
from custom_components.swisspower_dynpreis.const import (
    CONF_METHOD,
    CONF_TARIFF_NAME,
    CONF_TARIFF_TYPES,
    CONF_UPDATE_TIME,
    DOMAIN,
    METHOD_TARIFF_NAME,
)
from custom_components.swisspower_dynpreis.sensor import SwisspowerDynPreisStatSensor

TZ_NAME = "Europe/Zurich"
FETCH_TARGET = (
    "custom_components.swisspower_dynpreis.coordinator."
    "SwisspowerDynPreisApiClient.fetch_tariffs"
)

CURRENT_PRICE = "sensor.test_electricity_current_price"
AVG_TODAY = "sensor.test_electricity_average_price_today"
AVG_TOMORROW = "sensor.test_electricity_average_price_tomorrow"
NEXT_CHANGE = "sensor.test_electricity_next_change"


def local_tz():
    """Return the Europe/Zurich tzinfo used throughout the tests."""
    tz = dt_util.get_time_zone(TZ_NAME)
    assert tz is not None
    return tz


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Build a local (Europe/Zurich) datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=local_tz())


async def set_time_zone(hass: HomeAssistant, tz_name: str = TZ_NAME) -> None:
    """Set the Home Assistant time zone, across HA versions."""
    setter = getattr(hass.config, "async_set_time_zone", None)
    if setter is not None:
        await setter(tz_name)
    else:
        hass.config.set_time_zone(tz_name)


def day_slots(
    day: date,
    values: list[float],
    *,
    tariff_type: str = "electricity",
    component: str = "energy",
    slot_minutes: int = 60,
) -> list[dict[str, Any]]:
    """Build API price slots for one local day, in the ESIT response shape."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=local_tz())
    step = timedelta(minutes=slot_minutes)
    slots: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        start = midnight + index * step
        slots.append(
            {
                "start_timestamp": start.isoformat(),
                "end_timestamp": (start + step - timedelta(seconds=1)).isoformat(),
                tariff_type: [
                    {"component": component, "unit": "CHF/kWh", "value": value}
                ],
            }
        )
    return slots


def hourly(day: date, base: float = 0.10, step: float = 0.01) -> list[dict[str, Any]]:
    """24 hourly slots whose value encodes the hour: base + hour * step."""
    return day_slots(day, [round(base + hour * step, 4) for hour in range(24)])


def slots_from(
    start: datetime,
    values: list[float],
    *,
    tariff_type: str = "electricity",
    component: str = "energy",
    slot_minutes: int = 60,
) -> list[dict[str, Any]]:
    """Build slots from an absolute instant, stepping in real elapsed time.

    Unlike day_slots this walks UTC, so it stays correct across a DST change -
    which is what a real API returns, rather than a wall-clock time that does
    not exist.
    """
    step = timedelta(minutes=slot_minutes)
    cursor = dt_util.as_utc(start)
    slots: list[dict[str, Any]] = []
    for value in values:
        slots.append(
            {
                "start_timestamp": dt_util.as_local(cursor).isoformat(),
                "end_timestamp": dt_util.as_local(
                    cursor + step - timedelta(seconds=1)
                ).isoformat(),
                tariff_type: [
                    {"component": component, "unit": "CHF/kWh", "value": value}
                ],
            }
        )
        cursor += step
    return slots


class FakeApi:
    """Controllable stand-in for the ESIT API.

    Slots are stored per (day, tariff type) and only the requested type is
    returned, the way the real endpoint behaves - it takes tariff_type as a
    query parameter.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.slots: dict[tuple[date, str], list[dict[str, Any]]] = {}
        self.error: Exception | None = None
        # Tariff types that should fail while the others still answer. None
        # means self.error applies to every type.
        self.failing_types: set[str] | None = None

    def publish(
        self,
        day: date,
        slots: list[dict[str, Any]],
        *,
        tariff_type: str = "electricity",
    ) -> None:
        """Make one day's slots available for one tariff type."""
        self.slots[(day, tariff_type)] = slots

    def unpublish(self, day: date, *, tariff_type: str = "electricity") -> None:
        """Take a day's slots away again."""
        self.slots.pop((day, tariff_type), None)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        """Return the published slots of the requested type inside the window."""
        self.calls.append(kwargs)
        tariff_type: str = kwargs["tariff_type"]
        if self.error is not None and (
            self.failing_types is None or tariff_type in self.failing_types
        ):
            raise self.error

        start: datetime = kwargs["start"]
        end: datetime = kwargs["end"]
        prices: list[dict[str, Any]] = []
        for (day, published_type), slots in sorted(
            self.slots.items(), key=lambda item: (item[0][0], item[0][1])
        ):
            if published_type != tariff_type:
                continue
            for slot in slots:
                raw_start = slot.get("start_timestamp")
                if raw_start is None:
                    # A slot the API sent without a timestamp is always returned.
                    prices.append(slot)
                    continue
                slot_start = dt_util.parse_datetime(raw_start)
                assert slot_start is not None
                if start <= slot_start <= end:
                    prices.append(slot)
        return {"prices": prices}


def make_entry(
    update_time: str | None = "06:00",
    *,
    tariff_types: list[str] | None = None,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Build a tariff-name config entry."""
    extra = options or {}
    options = {}
    if update_time is not None:
        options[CONF_UPDATE_TIME] = update_time
    options.update(extra)
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            CONF_NAME: "Test",
            CONF_METHOD: METHOD_TARIFF_NAME,
            CONF_TARIFF_NAME: "D1",
            CONF_TARIFF_TYPES: tariff_types or ["electricity"],
        },
        options=options,
    )


@contextmanager
def all_entities_enabled() -> Iterator[None]:
    """Set up with the off-by-default entities switched on, as a user would.

    Only the entities whose state is a number are enabled by default, so the
    timestamp sensor and every on/off sensor are registered disabled and never
    reach the state machine - a test asserting on their state would find
    nothing. Patching the registry default is enough: Home Assistant reads it
    once, while the entity is being added.
    """
    with ExitStack() as stack:
        for cls in (SwisspowerDynPreisStatSensor, SwisspowerDynPreisBinarySensor):
            stack.enter_context(
                patch.object(cls, "entity_registry_enabled_default", True)
            )
        yield


async def setup_integration(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up the config entry. The api fixture patches the client."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def advance(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    total: timedelta,
    step: timedelta = timedelta(minutes=5),
) -> None:
    """Move the clock forward in small steps, firing HA time listeners."""
    elapsed = timedelta()
    while elapsed < total:
        chunk = min(step, total - elapsed)
        freezer.tick(chunk)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        elapsed += chunk
