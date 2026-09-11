"""The manual refresh button for Swisspower DynPreis.

Everything else in this integration is on a clock: two daily anchors, an
escalating hunt for prices that have not been published yet, and a render timer
that recomputes entity state at every price boundary. This is the one way to
say "now" - after fixing a token, after the supplier republishes a corrected
curve, or simply to check whether the API is answering at all.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_NAME, DOMAIN
from .coordinator import SwisspowerDynPreisCoordinator
from .entity import device_info_for


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Swisspower DynPreis buttons."""
    coordinator: SwisspowerDynPreisCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            SwisspowerDynPreisRefreshButton(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                name=entry.title or DEFAULT_NAME,
            )
        ]
    )


class SwisspowerDynPreisRefreshButton(ButtonEntity):
    """Fetch the tariffs again, right now, and re-render every entity.

    One per config entry, not one per tariff type: a refresh always fetches all
    configured types in the same cycle, so a button per type would only be five
    ways to do the identical thing.

    Deliberately not a CoordinatorEntity. Its state is the moment it was last
    pressed, which owes nothing to the price curve, and subscribing would write
    that unchanged state again at every price boundary - up to 96 times a day
    with quarter-hourly tariffs.
    """

    _attr_icon = "mdi:refresh"

    def __init__(
        self,
        *,
        coordinator: SwisspowerDynPreisCoordinator,
        entry_id: str,
        name: str,
    ) -> None:
        self._coordinator = coordinator
        self._entry_id = entry_id
        self._name = name

    @property
    def name(self) -> str:
        return f"{self._name} Refresh now"

    @property
    def unique_id(self) -> str:
        return f"{self._entry_id}_refresh"

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for(self._entry_id, self._name)

    @property
    def available(self) -> bool:
        """Always available - which is the whole point.

        The price entities go unavailable for a tariff type that has never
        returned anything, and CoordinatorEntity would tie this button to
        last_update_success as well. That is exactly backwards: a failed fetch
        is the moment a user most wants to press "refresh", and a greyed-out
        button would offer no way out of it.
        """
        return True

    async def async_press(self) -> None:
        """Fetch now, then say plainly whether it worked.

        The coordinator swallows fetch failures by design, because a tariff
        type that cannot be refreshed keeps serving its cached curve rather
        than going unavailable. That is right for the scheduled path and wrong
        for a deliberate press: without this, a press against a dead API or a
        rejected token would look exactly like a successful one. Raising turns
        it into an error the UI shows and an automation can catch.

        Judged per tariff type rather than on last_update_success, which stays
        True as long as there is a cache to serve - even in a cycle where every
        single type failed.

        A partial failure is deliberately not raised on: the refresh genuinely
        succeeded for the other types, and every current-price sensor already
        reports its own ``from_cache`` and ``last_successful_fetch``. The
        warning the coordinator logs names the types that stayed on cache.
        """
        await self._coordinator.async_refresh_now()

        failures = self._coordinator.failed_tariff_types()
        if not failures or len(failures) < len(self._coordinator.tariff_types):
            return

        raise HomeAssistantError(
            "Refreshing the Swisspower DynPreis tariffs failed: "
            + "; ".join(f"{key}: {value}" for key, value in sorted(failures.items()))
        )
