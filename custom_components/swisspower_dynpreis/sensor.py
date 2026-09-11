"""Sensors for Swisspower DynPreis."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_TARIFF_TYPES,
    DEFAULT_NAME,
    DOMAIN,
    TOMORROW_COVERAGE_RATIO,
    WINDOW_DAYS_FORWARD,
)
from .coordinator import SwisspowerDynPreisCoordinator
from .entity import SwisspowerDynPreisEntity, device_info_for
from .pricing import extract_slot_value, find_current_slot, normalize_price_slots
from .stats import (
    average_for_day,
    day_is_complete,
    day_stats,
    day_summaries,
    next_change,
    slots_for_day,
    upcoming_slots,
    window_attrs,
    window_value,
)


@dataclass(frozen=True)
class SensorDescription:
    """One derived numeric or timestamp sensor.

    ``enabled_default`` follows one rule across both platforms: an entity whose
    state is a number is on by default, everything else is off. A fresh install
    therefore starts with the price entities only, and the rest is one click
    away in the entity registry instead of five tariff types' worth of clutter.
    Home Assistant reads this at first registration, so an existing install
    keeps whatever it has enabled today.
    """

    key: str
    name: str
    enabled_default: bool
    unit: str | None
    value_fn: Callable[[list[dict[str, Any]], datetime, str, str | None], Any]
    extra_fn: (
        Callable[[list[dict[str, Any]], datetime, str, str | None], dict[str, Any]]
        | None
    ) = None
    device_class: SensorDeviceClass | None = None


_DERIVED_DESCRIPTIONS: tuple[SensorDescription, ...] = (
    SensorDescription(
        key="next_change",
        name="Next change",
        # The only entity here whose state is not a number: a timestamp. It is
        # still provided, but off by default like the on/off ones, so a fresh
        # install starts with the numeric price entities alone.
        enabled_default=False,
        unit=None,
        value_fn=lambda slots, now, tariff, component: next_change(
            slots, now, tariff, component
        ),
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    SensorDescription(
        key="avg_today",
        name="Average price today",
        enabled_default=True,
        unit="CHF/kWh",
        value_fn=lambda slots, now, tariff, component: average_for_day(
            slots, now, 0, tariff, component
        ),
        extra_fn=lambda slots, now, tariff, component: day_stats(
            slots, now, 0, tariff, component
        ),
    ),
    SensorDescription(
        key="avg_tomorrow",
        name="Average price tomorrow",
        enabled_default=True,
        unit="CHF/kWh",
        value_fn=lambda slots, now, tariff, component: average_for_day(
            slots, now, 1, tariff, component
        ),
        extra_fn=lambda slots, now, tariff, component: day_stats(
            slots, now, 1, tariff, component
        ),
    ),
)

# key, name, day offset, window length in hours, cheapest ("min") or dearest.
# Spelled out rather than generated so every entity key is greppable.
_WINDOW_SENSORS: tuple[tuple[str, str, int, int, str], ...] = (
    ("lowest_2h_today", "Lowest 2h window today", 0, 2, "min"),
    ("lowest_2h_tomorrow", "Lowest 2h window tomorrow", 1, 2, "min"),
    ("lowest_4h_today", "Lowest 4h window today", 0, 4, "min"),
    ("lowest_4h_tomorrow", "Lowest 4h window tomorrow", 1, 4, "min"),
    ("highest_2h_today", "Highest 2h window today", 0, 2, "max"),
    ("highest_2h_tomorrow", "Highest 2h window tomorrow", 1, 2, "max"),
    ("highest_4h_today", "Highest 4h window today", 0, 4, "max"),
    ("highest_4h_tomorrow", "Highest 4h window tomorrow", 1, 4, "max"),
)


def _window_description(
    key: str, name: str, offset_days: int, hours: int, extreme: str
) -> SensorDescription:
    """Build one of the lowest/highest window sensors."""
    return SensorDescription(
        key=key,
        name=name,
        enabled_default=True,
        unit="CHF/kWh",
        value_fn=lambda slots, now, tariff, component: window_value(
            slots, now, offset_days, hours, tariff, component, extreme
        ),
        extra_fn=lambda slots, now, tariff, component: window_attrs(
            slots, now, offset_days, hours, tariff, component, extreme
        ),
    )


SENSOR_DESCRIPTIONS: tuple[SensorDescription, ...] = _DERIVED_DESCRIPTIONS + tuple(
    _window_description(*row) for row in _WINDOW_SENSORS
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Swisspower DynPreis sensors."""
    coordinator: SwisspowerDynPreisCoordinator = hass.data[DOMAIN][entry.entry_id]
    name = entry.title or DEFAULT_NAME

    tariff_types = entry.data[CONF_TARIFF_TYPES]
    # SensorEntity rather than the integration's own base: the raw response
    # sensor below belongs to the entry, not to a tariff type, so it does not
    # inherit from it.
    entities: list[SensorEntity] = []
    component_map = _collect_components(coordinator.data, tariff_types)
    for tariff_type in tariff_types:
        entities.append(
            SwisspowerDynPreisCurrentPriceSensor(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                name=name,
                tariff_type=tariff_type,
            )
        )
        for component in sorted(component_map.get(tariff_type, set())):
            entities.append(
                SwisspowerDynPreisCurrentPriceSensor(
                    coordinator=coordinator,
                    entry_id=entry.entry_id,
                    name=name,
                    tariff_type=tariff_type,
                    component=component,
                )
            )
        entities.extend(
            SwisspowerDynPreisStatSensor(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                name=name,
                tariff_type=tariff_type,
                description=description,
            )
            for description in SENSOR_DESCRIPTIONS
        )

    # One for the whole entry, not one per tariff type: it carries every type's
    # answer in a single attribute, which is what makes it worth opening.
    entities.append(
        SwisspowerDynPreisRawResponseSensor(
            coordinator=coordinator,
            entry_id=entry.entry_id,
            name=name,
        )
    )

    async_add_entities(entities)


class SwisspowerDynPreisCurrentPriceSensor(SwisspowerDynPreisEntity, SensorEntity):
    """The price that applies right now."""

    # The whole price curve is an attribute, and state is now written at every
    # price boundary - up to 96 times a day with quarter-hourly tariffs. Left
    # in, the recorder would store the full curve on every one of those writes,
    # and there are now five curves rather than one. tomorrow_valid is
    # deliberately not in here: one bool that flips once a day is worth having
    # in the history.
    _unrecorded_attributes = frozenset(
        {"prices", "prices_today", "prices_tomorrow", "prices_upcoming", "price_days"}
    )

    def __init__(
        self,
        *,
        coordinator: SwisspowerDynPreisCoordinator,
        entry_id: str,
        name: str,
        tariff_type: str,
        component: str | None = None,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            entry_id=entry_id,
            name=name,
            tariff_type=tariff_type,
        )
        self._component = component

    @property
    def name(self) -> str:
        if self._component:
            return f"{self._name} {self._tariff_type} {self._component} Current price"
        return f"{self._name} {self._tariff_type} Current price"

    @property
    def unique_id(self) -> str:
        if self._component:
            return (
                f"{self._entry_id}_{self._tariff_type}_{self._component}_current_price"
            )
        return f"{self._entry_id}_{self._tariff_type}_current_price"

    @property
    def native_unit_of_measurement(self) -> str | None:
        return "CHF/kWh"

    @property
    def native_value(self) -> float | None:
        slot = find_current_slot(self.price_slots, dt_util.now())
        if not slot:
            return None
        return extract_slot_value(slot, self._tariff_type, self._component)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slots = self.price_slots
        # Read once. Every attribute below is derived from the same instant, so
        # a render cannot report a current slot from one clock reading and a
        # future curve from another - the boundary the render timer fires on is
        # exactly where those two would disagree.
        now = dt_util.now()
        slot = find_current_slot(slots, now)
        current_start = None
        current_end = None
        current_value = None
        if slot:
            current_start = slot.get("start_timestamp")
            current_end = slot.get("end_timestamp")
            current_value = extract_slot_value(slot, self._tariff_type, self._component)
        last_success = self.coordinator.last_success(self._tariff_type)
        # Normalized once and handed to every display helper below. This is
        # also the only place the component is applied: the raw "prices"
        # attribute is the unfiltered API payload, identical on every component
        # sensor of a tariff type, which is why a chart cannot use it directly.
        normalized = normalize_price_slots(slots, self._tariff_type, self._component)
        return {
            "tariff_type": self._tariff_type,
            "component": self._component,
            "prices": slots,
            # The chart-ready views: values already resolved for this entity's
            # tariff type and component, "end" still inclusive.
            "prices_today": slots_for_day(normalized, now, 0),
            "prices_tomorrow": slots_for_day(normalized, now, 1),
            "prices_upcoming": upcoming_slots(normalized, now),
            "price_days": day_summaries(normalized, now, WINDOW_DAYS_FORWARD),
            # So a dashboard can tell "tomorrow is not published yet" from
            # "tomorrow really is 0.00", which an empty list alone cannot say.
            # About this entity's own curve, not the coordinator's all-types
            # view: that one exists to decide whether to ask the API again.
            "tomorrow_valid": day_is_complete(
                normalized, now, 1, TOMORROW_COVERAGE_RATIO
            ),
            "current_start_timestamp": current_start,
            "current_end_timestamp": current_end,
            "current_value": current_value,
            # So an outage that is being masked by cached prices is still
            # visible, in the state machine and to automations.
            "last_successful_fetch": (
                last_success.isoformat() if last_success else None
            ),
            "from_cache": self.coordinator.tariff_is_stale(self._tariff_type),
        }


class SwisspowerDynPreisStatSensor(SwisspowerDynPreisEntity, SensorEntity):
    """A statistic derived from the cached price curve."""

    def __init__(
        self,
        *,
        coordinator: SwisspowerDynPreisCoordinator,
        entry_id: str,
        name: str,
        tariff_type: str,
        description: SensorDescription,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            entry_id=entry_id,
            name=name,
            tariff_type=tariff_type,
        )
        # Deliberately NOT called ``entity_description``: Home Assistant reads
        # its own fields (has_entity_name, suggested_unit_of_measurement, ...)
        # off that attribute whenever it exists, and SensorDescription is not a
        # SensorEntityDescription. Naming it that way made every stat entity
        # fail to be added with an AttributeError.
        self._description = description

    @property
    def name(self) -> str:
        return f"{self._name} {self._tariff_type} {self._description.name}"

    @property
    def unique_id(self) -> str:
        return f"{self._entry_id}_{self._tariff_type}_{self._description.key}"

    @property
    def native_unit_of_measurement(self) -> str | None:
        return self._description.unit

    @property
    def device_class(self) -> SensorDeviceClass | None:
        return self._description.device_class

    @property
    def entity_registry_enabled_default(self) -> bool:
        return self._description.enabled_default

    @property
    def native_value(self) -> Any:
        return self._description.value_fn(
            self.price_slots, dt_util.now(), self._tariff_type, None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self._description.extra_fn:
            return {}
        return self._description.extra_fn(
            self.price_slots, dt_util.now(), self._tariff_type, None
        )


class SwisspowerDynPreisRawResponseSensor(
    CoordinatorEntity[SwisspowerDynPreisCoordinator], SensorEntity
):
    """The API's answers as received, before this integration touches them.

    Every other entity shows a value the integration computed. When one of them
    looks wrong the next question is always the same - did the API send that, or
    did we make it up - and until now the only way to answer it was to turn on
    debug logging and wait for the next fetch. This holds the last answer per
    tariff type in an attribute, so the question is answerable now.

    Off by default, and that is not caution about clutter: the payload is the
    whole price curve for every configured type, tens of kilobytes of it, and
    Home Assistant pushes an entity's attributes to every open browser tab on
    every state write. Nobody should pay that who is not currently debugging.

    The state is the payload's size rather than the payload: a state is capped
    at 255 characters, and a number here is the useful summary anyway - an API
    that answered with nothing is visible at a glance, and the size can be
    graphed.

    "Raw" means the decoded body before the coordinator normalizes it. The
    client does two things first, and both preserve rather than reshape: a body
    that is not JSON arrives as {"raw": "<text>"}, and a JSON document whose top
    level is not an object arrives as {"data": ...}. Nothing is redacted, so the
    payload is whatever the API chose to send back - treat it as customer data.
    """

    # Same reason as on the current price sensor, only more so: this is the
    # largest attribute the integration has, and writing it to the long-term
    # database on every fetch would be the single most expensive thing it does.
    # The recorder refuses attributes over 16 KB anyway and would log a warning
    # about it every single time.
    _unrecorded_attributes = frozenset({"responses", "captured", "bytes"})

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES
    _attr_icon = "mdi:code-json"

    def __init__(
        self,
        *,
        coordinator: SwisspowerDynPreisCoordinator,
        entry_id: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._name = name
        self._written_capture: dict[str, datetime] = {}

    @property
    def name(self) -> str:
        return f"{self._name} API response"

    @property
    def unique_id(self) -> str:
        return f"{self._entry_id}_api_response"

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for(self._entry_id, self._name)

    @property
    def available(self) -> bool:
        """Always available.

        A diagnostic entity that disappears when the integration is unhappy is
        useless at the one moment it is meant to help, and the captured payload
        stays readable whether or not the newest fetch worked.
        """
        return True

    async def async_added_to_hass(self) -> None:
        """Record what the entity's first state write already showed.

        That write happens through the platform, not through the coordinator
        update path below, so without this the next render tick would find an
        empty _written_capture and repeat it once for nothing.
        """
        await super().async_added_to_hass()
        self._written_capture = dict(self.coordinator.raw_captured)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state only when a fetch actually brought something new.

        The coordinator notifies its listeners at every price boundary too, so
        the base class would rewrite this entity - the largest attribute payload
        in the integration - up to 96 times a day for a value that changes twice.

        The capture timestamps are the cheap stand-in for the payload: they are
        set by the same line that stores it, so they move exactly when it does.
        """
        captured = dict(self.coordinator.raw_captured)
        if captured == self._written_capture:
            return
        self._written_capture = captured
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> int | None:
        return self.coordinator.raw_total_bytes()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.raw_response_state()


def _collect_components(
    data: dict[str, Any] | None,
    tariff_types: list[str],
) -> dict[str, set[str]]:
    """Find the price components the API returned per tariff type."""
    components: dict[str, set[str]] = {
        tariff_type: set() for tariff_type in tariff_types
    }
    if not isinstance(data, dict):
        return components
    for tariff_type in tariff_types:
        tariff_data = data.get(tariff_type)
        if not isinstance(tariff_data, dict):
            continue
        slots = tariff_data.get("prices")
        if not isinstance(slots, list):
            continue
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            prices = slot.get(tariff_type)
            if isinstance(prices, list):
                for price in prices:
                    if not isinstance(price, dict):
                        continue
                    component = price.get("component")
                    if isinstance(component, str) and component:
                        components[tariff_type].add(component)
            component = slot.get("component")
            if isinstance(component, str) and component:
                components[tariff_type].add(component)
    return components
