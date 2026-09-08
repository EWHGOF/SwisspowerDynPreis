"""Shared entity plumbing for the Swisspower DynPreis platforms."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SwisspowerDynPreisCoordinator


class SwisspowerDynPreisEntity(CoordinatorEntity[SwisspowerDynPreisCoordinator]):
    """Base for every entity of one tariff type."""

    _attr_should_poll = False

    def __init__(
        self,
        *,
        coordinator: SwisspowerDynPreisCoordinator,
        entry_id: str,
        name: str,
        tariff_type: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._name = name
        self._tariff_type = tariff_type

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=self._name,
            manufacturer="Swisspower",
        )

    @property
    def price_slots(self) -> list[dict[str, Any]]:
        """Return the cached slots for this tariff type, or an empty list.

        Guards every shape the API and the first refresh can produce: no data
        yet, a tariff type the response omitted, or a payload whose prices key
        is not a list.
        """
        data = self.coordinator.data
        if not isinstance(data, dict):
            return []
        tariff_data = data.get(self._tariff_type)
        if not isinstance(tariff_data, dict):
            return []
        prices = tariff_data.get("prices")
        return prices if isinstance(prices, list) else []
