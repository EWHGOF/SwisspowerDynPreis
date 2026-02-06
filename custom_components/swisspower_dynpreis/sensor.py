"""Sensors for Swisspower DynPreis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_TARIFF_TYPES, DEFAULT_NAME, DOMAIN
from .coordinator import SwisspowerDynPreisCoordinator
from .pricing import (
    average_price_for_window,
    day_bounds,
    extract_slot_value,
    find_current_slot,
    normalize_price_slots,
    percentile_threshold,
    window_extreme,
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
    entities: list[CoordinatorEntity] = []
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
            _build_stat_entities(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                name=name,
                tariff_type=tariff_type,
            )
        )

    async_add_entities(entities)


@dataclass(frozen=True)
class _StatSensorDescription:
    key: str
    name: str
    enabled_default: bool
    unit: str | None
    kind: str
    value_fn: Callable[[list[dict[str, Any]], datetime, str, str | None], Any]
    extra_fn: Callable[[list[dict[str, Any]], datetime, str, str | None], dict[str, Any]] | None = None
    device_class: SensorDeviceClass | None = None


STAT_SENSORS: tuple[_StatSensorDescription, ...] = (
    _StatSensorDescription(
        key="next_change",
        name="Next change",
        enabled_default=True,
        unit=None,
        kind="timestamp",
        value_fn=lambda slots, now, tariff, component: _next_change(slots, now, tariff, component),
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    _StatSensorDescription(
        key="avg_today",
        name="Average price today",
        enabled_default=True,
        unit="CHF/kWh",
        kind="sensor",
        value_fn=lambda slots, now, tariff, component: _average_for_day(slots, 0, tariff, component),
        extra_fn=lambda slots, now, tariff, component: _day_stats(slots, 0, tariff, component),
    ),
    _StatSensorDescription(
        key="avg_tomorrow",
        name="Average price tomorrow",
        enabled_default=True,
        unit="CHF/kWh",
        kind="sensor",
        value_fn=lambda slots, now, tariff, component: _average_for_day(slots, 1, tariff, component),
        extra_fn=lambda slots, now, tariff, component: _day_stats(slots, 1, tariff, component),
    ),
    _StatSensorDescription(
        key="lowest_2h_today",
        name="Lowest 2h window today",
        enabled_default=True,
        unit="CHF/kWh",
        kind="sensor",
        value_fn=lambda slots, now, tariff, component: _window_value(slots, 0, 2, tariff, component, "min"),
        extra_fn=lambda slots, now, tariff, component: _window_attrs(slots, 0, 2, tariff, component, "min"),
    ),
    _StatSensorDescription(
        key="lowest_2h_tomorrow",
        name="Lowest 2h window tomorrow",
        enabled_default=True,
        unit="CHF/kWh",
        kind="sensor",
        value_fn=lambda slots, now, tariff, component: _window_value(slots, 1, 2, tariff, component, "min"),
        extra_fn=lambda slots, now, tariff, component: _window_attrs(slots, 1, 2, tariff, component, "min"),
    ),
    _StatSensorDescription(
        key="lowest_4h_today",
        name="Lowest 4h window today",
        enabled_default=True,
        unit="CHF/kWh",
        kind="sensor",
        value_fn=lambda slots, now, tariff, component: _window_value(slots, 0, 4, tariff, component, "min"),
        extra_fn=lambda slots, now, tariff, component: _window_attrs(slots, 0, 4, tariff, component, "min"),
    ),
    _StatSensorDescription(
        key="lowest_4h_tomorrow",
        name="Lowest 4h window tomorrow",
        enabled_default=True,
        unit="CHF/kWh",
        kind="sensor",
        value_fn=lambda slots, now, tariff, component: _window_value(slots, 1, 4, tariff, component, "min"),
        extra_fn=lambda slots, now, tariff, component: _window_attrs(slots, 1, 4, tariff, component, "min"),
    ),
    _StatSensorDescription(
        key="highest_2h_today",
        name="Highest 2h window today",
        enabled_default=True,
        unit="CHF/kWh",
        kind="sensor",
        value_fn=lambda slots, now, tariff, component: _window_value(slots, 0, 2, tariff, component, "max"),
        extra_fn=lambda slots, now, tariff, component: _window_attrs(slots, 0, 2, tariff, component, "max"),
    ),
    _StatSensorDescription(
        key="highest_2h_tomorrow",
        name="Highest 2h window tomorrow",
        enabled_default=True,
        unit="CHF/kWh",
        kind="sensor",
        value_fn=lambda slots, now, tariff, component: _window_value(slots, 1, 2, tariff, component, "max"),
        extra_fn=lambda slots, now, tariff, component: _window_attrs(slots, 1, 2, tariff, component, "max"),
    ),
    _StatSensorDescription(
        key="highest_4h_today",
        name="Highest 4h window today",
        enabled_default=True,
        unit="CHF/kWh",
        kind="sensor",
        value_fn=lambda slots, now, tariff, component: _window_value(slots, 0, 4, tariff, component, "max"),
        extra_fn=lambda slots, now, tariff, component: _window_attrs(slots, 0, 4, tariff, component, "max"),
    ),
    _StatSensorDescription(
        key="highest_4h_tomorrow",
        name="Highest 4h window tomorrow",
        enabled_default=True,
        unit="CHF/kWh",
        kind="sensor",
        value_fn=lambda slots, now, tariff, component: _window_value(slots, 1, 4, tariff, component, "max"),
        extra_fn=lambda slots, now, tariff, component: _window_attrs(slots, 1, 4, tariff, component, "max"),
    ),
    _StatSensorDescription(
        key="cheapest_25_today",
        name="Cheapest 25% hours today",
        enabled_default=True,
        unit=None,
        kind="binary",
        value_fn=lambda slots, now, tariff, component: _percentile_binary(slots, 0.25, tariff, component, False),
    ),
    _StatSensorDescription(
        key="cheapest_10_today",
        name="Cheapest 10% hours today",
        enabled_default=True,
        unit=None,
        kind="binary",
        value_fn=lambda slots, now, tariff, component: _percentile_binary(slots, 0.10, tariff, component, False),
    ),
    _StatSensorDescription(
        key="cheapest_50_today",
        name="Cheapest 50% hours today",
        enabled_default=True,
        unit=None,
        kind="binary",
        value_fn=lambda slots, now, tariff, component: _percentile_binary(slots, 0.50, tariff, component, False),
    ),
    _StatSensorDescription(
        key="expensive_25_today",
        name="Most expensive 25% hours today",
        enabled_default=True,
        unit=None,
        kind="binary",
        value_fn=lambda slots, now, tariff, component: _percentile_binary(slots, 0.25, tariff, component, True),
    ),
    _StatSensorDescription(
        key="expensive_10_today",
        name="Most expensive 10% hours today",
        enabled_default=True,
        unit=None,
        kind="binary",
        value_fn=lambda slots, now, tariff, component: _percentile_binary(slots, 0.10, tariff, component, True),
    ),
    _StatSensorDescription(
        key="in_cheapest_2h_today",
        name="In cheapest 2h window today",
        enabled_default=True,
        unit=None,
        kind="binary",
        value_fn=lambda slots, now, tariff, component: _in_window(slots, now, 0, 2, tariff, component, "min"),
    ),
    _StatSensorDescription(
        key="in_cheapest_4h_today",
        name="In cheapest 4h window today",
        enabled_default=True,
        unit=None,
        kind="binary",
        value_fn=lambda slots, now, tariff, component: _in_window(slots, now, 0, 4, tariff, component, "min"),
    ),
    _StatSensorDescription(
        key="in_expensive_2h_today",
        name="In most expensive 2h window today",
        enabled_default=True,
        unit=None,
        kind="binary",
        value_fn=lambda slots, now, tariff, component: _in_window(slots, now, 0, 2, tariff, component, "max"),
    ),
    _StatSensorDescription(
        key="in_expensive_4h_today",
        name="In most expensive 4h window today",
        enabled_default=True,
        unit=None,
        kind="binary",
        value_fn=lambda slots, now, tariff, component: _in_window(slots, now, 0, 4, tariff, component, "max"),
    ),
)


def _build_stat_entities(
    *,
    coordinator: SwisspowerDynPreisCoordinator,
    entry_id: str,
    name: str,
    tariff_type: str,
) -> list[CoordinatorEntity]:
    entities: list[CoordinatorEntity] = []
    for description in STAT_SENSORS:
        entity_cls: type[CoordinatorEntity]
        if description.kind == "binary":
            entity_cls = SwisspowerDynPreisBinaryStatSensor
        else:
            entity_cls = SwisspowerDynPreisStatSensor
        entities.append(
            entity_cls(
                coordinator=coordinator,
                entry_id=entry_id,
                name=name,
                tariff_type=tariff_type,
                description=description,
            )
        )
    return entities


class SwisspowerDynPreisCurrentPriceSensor(
    CoordinatorEntity[SwisspowerDynPreisCoordinator], SensorEntity
):
    """Representation of a Swisspower DynPreis tariff."""

    _attr_should_poll = False

    def __init__(
        self,
        *,
        coordinator: SwisspowerDynPreisCoordinator,
        entry_id: str,
        name: str,
        tariff_type: str,
        component: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._name = name
        self._tariff_type = tariff_type
        self._component = component

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=self._name,
            manufacturer="Swisspower",
        )

    @property
    def name(self) -> str:
        if self._component:
            return f"{self._name} {self._tariff_type} {self._component} Current price"
        return f"{self._name} {self._tariff_type} Current price"

    @property
    def unique_id(self) -> str:
        if self._component:
            return f"{self._entry_id}_{self._tariff_type}_{self._component}_current_price"
        return f"{self._entry_id}_{self._tariff_type}_current_price"

    @property
    def native_unit_of_measurement(self) -> str | None:
        return "CHF/kWh"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data.get(self._tariff_type, {})
        slot = find_current_slot(data.get("prices", []), dt_util.now())
        if not slot:
            return None
        return extract_slot_value(slot, self._tariff_type, self._component)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self._tariff_type, {})
        slot = find_current_slot(data.get("prices", []), dt_util.now())
        current_start = None
        current_end = None
        current_value = None
        if slot:
            current_start = slot.get("start_timestamp")
            current_end = slot.get("end_timestamp")
            current_value = extract_slot_value(slot, self._tariff_type, self._component)
        return {
            "tariff_type": self._tariff_type,
            "component": self._component,
            "prices": data.get("prices", []),
            "current_start_timestamp": current_start,
            "current_end_timestamp": current_end,
            "current_value": current_value,
        }


class SwisspowerDynPreisStatSensor(
    CoordinatorEntity[SwisspowerDynPreisCoordinator], SensorEntity
):
    """Statistics sensor for Swisspower DynPreis."""

    _attr_should_poll = False

    def __init__(
        self,
        *,
        coordinator: SwisspowerDynPreisCoordinator,
        entry_id: str,
        name: str,
        tariff_type: str,
        description: _StatSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._name = name
        self._tariff_type = tariff_type
        self.entity_description = description

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=self._name,
            manufacturer="Swisspower",
        )

    @property
    def name(self) -> str:
        return f"{self._name} {self._tariff_type} {self.entity_description.name}"

    @property
    def unique_id(self) -> str:
        return f"{self._entry_id}_{self._tariff_type}_{self.entity_description.key}"

    @property
    def native_unit_of_measurement(self) -> str | None:
        return self.entity_description.unit

    @property
    def device_class(self) -> SensorDeviceClass | None:
        return self.entity_description.device_class

    @property
    def entity_registry_enabled_default(self) -> bool:
        return self.entity_description.enabled_default

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data.get(self._tariff_type, {})
        now = dt_util.now()
        return self.entity_description.value_fn(
            data.get("prices", []),
            now,
            self._tariff_type,
            None,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.entity_description.extra_fn:
            return {}
        data = self.coordinator.data.get(self._tariff_type, {})
        now = dt_util.now()
        return self.entity_description.extra_fn(
            data.get("prices", []),
            now,
            self._tariff_type,
            None,
        )


class SwisspowerDynPreisBinaryStatSensor(
    CoordinatorEntity[SwisspowerDynPreisCoordinator], BinarySensorEntity
):
    """Binary statistics sensor for Swisspower DynPreis."""

    _attr_should_poll = False

    def __init__(
        self,
        *,
        coordinator: SwisspowerDynPreisCoordinator,
        entry_id: str,
        name: str,
        tariff_type: str,
        description: _StatSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._name = name
        self._tariff_type = tariff_type
        self.entity_description = description

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=self._name,
            manufacturer="Swisspower",
        )

    @property
    def name(self) -> str:
        return f"{self._name} {self._tariff_type} {self.entity_description.name}"

    @property
    def unique_id(self) -> str:
        return f"{self._entry_id}_{self._tariff_type}_{self.entity_description.key}"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return self.entity_description.enabled_default

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data.get(self._tariff_type, {})
        now = dt_util.now()
        return bool(
            self.entity_description.value_fn(
                data.get("prices", []),
                now,
                self._tariff_type,
                None,
            )
        )


def _next_change(
    slots: list[dict[str, Any]],
    now: datetime,
    tariff_type: str,
    component: str | None,
) -> datetime | None:
    slot = find_current_slot(slots, now)
    if not slot:
        return None
    return dt_util.parse_datetime(slot.get("end_timestamp"))


def _average_for_day(
    slots: list[dict[str, Any]],
    offset_days: int,
    tariff_type: str,
    component: str | None,
) -> float | None:
    normalized = normalize_price_slots(slots, tariff_type, component)
    start, end_exclusive = day_bounds(offset_days)
    return average_price_for_window(normalized, start, end_exclusive)


def _day_stats(
    slots: list[dict[str, Any]],
    offset_days: int,
    tariff_type: str,
    component: str | None,
) -> dict[str, Any]:
    normalized = normalize_price_slots(slots, tariff_type, component)
    start, end_exclusive = day_bounds(offset_days)
    values = [slot.value for slot in normalized if start <= slot.start < end_exclusive]
    if not values:
        return {}
    return {
        "min_price": min(values),
        "max_price": max(values),
        "average_price": sum(values) / len(values),
        "slots": len(values),
    }


def _window_value(
    slots: list[dict[str, Any]],
    offset_days: int,
    window_hours: int,
    tariff_type: str,
    component: str | None,
    extreme: str,
) -> float | None:
    normalized = normalize_price_slots(slots, tariff_type, component)
    start, end_exclusive = day_bounds(offset_days)
    result = window_extreme(normalized, start, end_exclusive, window_hours, extreme=extreme)
    if not result:
        return None
    return result[0]


def _window_attrs(
    slots: list[dict[str, Any]],
    offset_days: int,
    window_hours: int,
    tariff_type: str,
    component: str | None,
    extreme: str,
) -> dict[str, Any]:
    normalized = normalize_price_slots(slots, tariff_type, component)
    start, end_exclusive = day_bounds(offset_days)
    result = window_extreme(normalized, start, end_exclusive, window_hours, extreme=extreme)
    if not result:
        return {}
    avg, window_start, window_end = result
    return {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_average": avg,
    }


def _percentile_binary(
    slots: list[dict[str, Any]],
    percentile: float,
    tariff_type: str,
    component: str | None,
    highest: bool,
) -> bool | None:
    normalized = normalize_price_slots(slots, tariff_type, component)
    start, end_exclusive = day_bounds(0)
    day_slots = [slot for slot in normalized if start <= slot.start < end_exclusive]
    if not day_slots:
        return None
    now = dt_util.now()
    current = next((slot for slot in day_slots if slot.start <= now <= slot.end), None)
    if not current:
        return None
    threshold = percentile_threshold([slot.value for slot in day_slots], percentile, highest=highest)
    if threshold is None:
        return None
    if highest:
        return current.value >= threshold
    return current.value <= threshold


def _in_window(
    slots: list[dict[str, Any]],
    now: datetime,
    offset_days: int,
    window_hours: int,
    tariff_type: str,
    component: str | None,
    extreme: str,
) -> bool | None:
    normalized = normalize_price_slots(slots, tariff_type, component)
    start, end_exclusive = day_bounds(offset_days)
    result = window_extreme(normalized, start, end_exclusive, window_hours, extreme=extreme)
    if not result:
        return None
    _, window_start, window_end = result
    return window_start <= now <= window_end


def _collect_components(
    data: dict[str, Any],
    tariff_types: list[str],
) -> dict[str, set[str]]:
    components: dict[str, set[str]] = {tariff_type: set() for tariff_type in tariff_types}
    for tariff_type in tariff_types:
        tariff_data = data.get(tariff_type, {})
        slots = tariff_data.get("prices", [])
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
