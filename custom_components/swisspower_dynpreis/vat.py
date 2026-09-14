"""Value-added tax on top of the net prices the API publishes.

The ESIT endpoint answers with prices excluding VAT. Whether VAT belongs on top
of them is a property of the customer, not of the tariff, so it is configured
here rather than guessed: a rate typed into the options, or - because the Swiss
rate has changed twice in recent years and a household may want it to follow
something it already maintains - the state of an entity, typically an
``input_number``.

These are pure functions of their arguments. Reading the entity and deciding
which rate currently applies is the coordinator's job, because only it has a
Home Assistant instance to read a state from and listeners to notify when the
rate moves.
"""

from __future__ import annotations

from typing import Any

from .const import MAX_VAT_RATE, VAT_DECIMALS


def parse_vat_rate(value: Any) -> float | None:
    """Read a VAT rate in percent, or None if the value cannot be one.

    None means "no usable rate here" and is deliberately distinct from 0.0,
    which is a rate the user really chose. The callers rely on that difference:
    an entity that is ``unknown`` must not be read as "VAT is now zero" and
    knock 8.1% off every price until it comes back.

    Rejected are the two placeholder states Home Assistant writes for an entity
    that cannot answer, anything that is not a number, a negative rate, and a
    rate above MAX_VAT_RATE - a sensor reporting 810 for 8.1% would otherwise
    multiply every price by nine.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().rstrip("%").strip()
        if not text or text.lower() in ("unknown", "unavailable", "none"):
            return None
        value = text
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if rate != rate or rate in (float("inf"), float("-inf")):
        return None
    if rate < 0 or rate > MAX_VAT_RATE:
        return None
    return rate


def vat_multiplier(rate: float | None) -> float:
    """Return the factor a net price is multiplied by for a rate in percent."""
    if not rate:
        return 1.0
    return 1.0 + rate / 100.0


def apply_multiplier(value: float | None, multiplier: float) -> float | None:
    """Scale one price, leaving a missing price missing.

    At a multiplier of exactly 1.0 the value is handed back untouched rather
    than rounded, so an installation that does not use this feature keeps the
    number the API sent, to the last bit.
    """
    if value is None or multiplier == 1.0:
        return value
    return round(value * multiplier, VAT_DECIMALS)
