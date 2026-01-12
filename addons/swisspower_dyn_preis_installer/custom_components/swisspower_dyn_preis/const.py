"""Constants for the Swisspower dynamic price integration."""

DOMAIN = "swisspower_dyn_preis"
PLATFORMS = ["sensor"]

CONF_METERING_CODE = "metering_code"
CONF_TARIFF_NAME = "tariff_name"
CONF_TARIFF_TYPE = "tariff_type"
CONF_TOKEN = "token"

DEFAULT_TARIFF_TYPE = "integrated"
TARIFF_TYPES = ["electricity", "grid", "dso", "integrated", "feed_in"]

API_BASE_URL = "https://esit.code-fabrik.ch/api/v1"
DEFAULT_UPDATE_INTERVAL = 15
