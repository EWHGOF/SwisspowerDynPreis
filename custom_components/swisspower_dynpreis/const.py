"""Constants for the Swisspower DynPreis integration."""

DOMAIN = "swisspower_dynpreis"
DEFAULT_NAME = "Swisspower DynPreis"

CONF_METHOD = "method"
CONF_API_URL = "api_url"
CONF_METERING_CODE = "metering_code"
CONF_TARIFF_NAME = "tariff_name"
CONF_TARIFF_TYPES = "tariff_types"
CONF_TOKEN = "token"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_QUERY_YEAR = "query_year"

METHOD_METERING_CODE = "metering_code"
METHOD_TARIFF_NAME = "tariff_name"

TARIFF_TYPES = ["electricity", "grid", "dso", "integrated", "feed_in"]

API_BASE = "https://esit.code-fabrik.ch/api/v1"
DEFAULT_UPDATE_INTERVAL = 60
TIMEOUT_SECONDS = 20
