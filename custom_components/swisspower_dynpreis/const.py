"""Constants for the Swisspower DynPreis integration."""

DOMAIN = "swisspower_dynpreis"
DEFAULT_NAME = "Swisspower DynPreis"

CONF_METHOD = "method"
CONF_API_URL = "api_url"
CONF_METERING_CODE = "metering_code"
CONF_TARIFF_NAME = "tariff_name"
CONF_TARIFF_TYPES = "tariff_types"
CONF_TOKEN = "token"
CONF_UPDATE_TIME = "update_time"
CONF_UPDATE_TIME_PM = "update_time_pm"
CONF_UPDATE_TIME_EVENING = "update_time_evening"
CONF_QUERY_YEAR = "query_year"
CONF_VAT_RATE = "vat_rate"
CONF_VAT_ENTITY = "vat_entity"
CONF_VAT_TARIFF_TYPES = "vat_tariff_types"

METHOD_METERING_CODE = "metering_code"
METHOD_TARIFF_NAME = "tariff_name"

TARIFF_TYPES = ["electricity", "grid", "dso", "integrated", "feed_in"]

API_BASE = "https://esit.code-fabrik.ch/api/v1"
# The three daily fetch times, asked for during setup and editable in the
# options. The second one also gates the hunt for tomorrow's prices (see
# _hunt_interval), because day-ahead prices are published in the afternoon;
# the third catches a supplier that publishes late without waiting for the
# hunt ladder to get there.
DEFAULT_UPDATE_TIME = "01:00"
DEFAULT_UPDATE_TIME_PM = "14:00"
DEFAULT_UPDATE_TIME_EVENING = "18:30"
TIMEOUT_SECONDS = 20

# The API publishes net prices. The integration adds VAT on top of them, at a
# rate that is either typed into the options or read from an entity - an
# input_number or a sensor - so a rate change is one entity write rather than a
# reconfiguration. Zero, the default, leaves every price exactly as it arrives.
DEFAULT_VAT_RATE = 0.0

# Sanity bound for a rate, whether typed in or read from an entity. A sensor
# that reports 810 instead of 8.1 would otherwise multiply every price by nine
# without anything looking wrong.
MAX_VAT_RATE = 100.0

# Decimals a price is rounded to after VAT has been applied. The API publishes
# four, and multiplying by 1.081 otherwise turns 0.1475 into
# 0.15944750000000002 in every attribute and every chart tooltip.
VAT_DECIMALS = 6

# Tariff types VAT is not applied to unless the user asks for it. A feed-in
# tariff is a credit to the customer, not a purchase, so a household that is
# not itself registered for VAT never adds it there - and silently inflating
# that number by 8.1% would be a wrong price, not a rounding detail.
VAT_EXEMPT_TARIFF_TYPES = ("feed_in",)

# How many days ahead the request window reaches. The window runs from the
# start of the local day to the start of the local day this many days later,
# so day 0 is today and days 1 to 3 are the three days ahead.
#
# Asking for more days than the supplier has published is not a new thing this
# value does: the window has always reached into tomorrow, and every morning
# before the afternoon publication it comes back with today only. A day-ahead
# supplier will simply leave the trailing days empty, and price_days reports
# their coverage as 0. It also sets how many days price_days summarizes.
#
# Three days ahead rather than two so a supplier that publishes further out can
# feed a longer optimization horizon. It costs nothing extra in requests: the
# window is a parameter of the same call, and the hunt ladder still chases only
# tomorrow (see tomorrow_complete), because that is the last day a day-ahead
# supplier will ever publish.
WINDOW_DAYS_FORWARD = 4

# Escalating wait after a failed fetch, in minutes. The chain gives up after
# BACKOFF_MAX_TRIES consecutive failures and waits for the next daily anchor,
# so a permanently broken API cannot turn into an unbounded request stream.
BACKOFF_MINUTES = (1, 2, 5, 10, 30)
BACKOFF_MAX_TRIES = 6

# Upper bound for a Retry-After header, so a broken or hostile value cannot
# park the integration for days.
MAX_RETRY_AFTER_SECONDS = 6 * 60 * 60

# While tomorrow's prices are still missing, retry on this escalating schedule
# (minutes) after the afternoon anchor has passed, and stop for the day at
# HUNT_STOP_HOUR local time.
HUNT_MINUTES = (30, 30, 60, 60, 120, 120)
HUNT_STOP_HOUR = 23

# When no slot covers the current instant, today's own prices are missing. That
# is worse than a missing next day and is not tied to a publication time, so it
# gets its own faster schedule (minutes) with no time-of-day gate. An API that
# answers with an empty list is a success, not a failure, so the failure backoff
# would never see this case.
TODAY_HUNT_MINUTES = (5, 10, 15, 30, 30, 60, 60, 120)

# Share of tomorrow's local day that must be covered by price slots before
# tomorrow counts as published. Measured in seconds of coverage rather than
# slot count, so it works for any slot length and for 23/24/25-hour days.
TOMORROW_COVERAGE_RATIO = 0.95
