"""Diagnostics for Swisspower DynPreis.

The point of this file is that "morgen bleibt leer" becomes answerable. It
shows the schedule's decisions and how each tariff type last fared, without
adding a single entity state write - and without the credentials.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_METERING_CODE, CONF_TOKEN, DOMAIN
from .coordinator import SwisspowerDynPreisCoordinator

TO_REDACT = {CONF_TOKEN, CONF_METERING_CODE}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: SwisspowerDynPreisCoordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data if isinstance(coordinator.data, dict) else {}

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "schedule": coordinator.schedule_state(),
        "tariff_types": {
            tariff_type: {
                "last_successful_fetch": (
                    status.last_success.isoformat() if status.last_success else None
                ),
                "last_error": status.last_error,
                "serving_from_cache": coordinator.tariff_is_stale(tariff_type),
                "slot_count": len(
                    data.get(tariff_type, {}).get("prices", [])
                    if isinstance(data.get(tariff_type), dict)
                    else []
                ),
            }
            for tariff_type, status in sorted(coordinator.tariff_status.items())
        },
    }
