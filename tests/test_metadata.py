"""Tests for the packaging metadata, so CI is not the only thing checking it."""

from __future__ import annotations

import json
import re
import string
from pathlib import Path

import pytest

INTEGRATION = Path(__file__).parent.parent / "custom_components" / "swisspower_dynpreis"
REPO = Path(__file__).parent.parent

# hassfest sorts manifest keys with "domain" as ".domain" and "name" as ".name",
# so those come first and everything else follows alphabetically. An unsorted
# manifest is an error, not a warning.
_SORT_OVERRIDES = {"domain": ".domain", "name": ".name"}


def _manifest() -> dict:
    return json.loads((INTEGRATION / "manifest.json").read_text())


def _flat_keys(data: dict, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in data.items():
        if isinstance(value, dict):
            keys |= _flat_keys(value, f"{prefix}{key}.")
        else:
            keys.add(f"{prefix}{key}")
    return keys


def test_manifest_keys_are_sorted_the_way_hassfest_wants() -> None:
    keys = list(_manifest())
    expected = sorted(keys, key=lambda key: _SORT_OVERRIDES.get(key, key))
    assert keys == expected, f"expected order: {expected}"


def test_manifest_has_the_keys_a_custom_integration_needs() -> None:
    manifest = _manifest()
    for key in ("domain", "name", "version", "documentation", "iot_class"):
        assert manifest.get(key), f"manifest is missing {key}"


def test_manifest_domain_matches_the_directory() -> None:
    assert _manifest()["domain"] == INTEGRATION.name


@pytest.mark.parametrize("key", ["documentation", "issue_tracker"])
def test_manifest_urls_point_at_this_repository(key: str) -> None:
    url = _manifest()[key]
    assert url.startswith("https://"), url
    assert "github.com/EWHGOF/SwisspowerDynPreis" in url, url


def test_hacs_json_has_only_keys_hacs_understands() -> None:
    """An unknown key makes the HACS validator fail."""
    allowed = {
        "name",
        "content_in_root",
        "country",
        "filename",
        "hacs",
        "hide_default_branch",
        "homeassistant",
        "persistent_directory",
        "render_readme",
        "zip_release",
    }
    data = json.loads((REPO / "hacs.json").read_text())
    assert set(data) <= allowed, f"unknown keys: {set(data) - allowed}"
    assert data["name"]


def test_render_readme_has_a_readme_to_render() -> None:
    assert (REPO / "README.md").is_file()


def test_hacs_minimum_matches_the_oldest_tested_version() -> None:
    """hacs.json promises a minimum; CI has to actually test it."""
    minimum = json.loads((REPO / "hacs.json").read_text())["homeassistant"]
    workflow = (REPO / ".github" / "workflows" / "tests.yml").read_text()
    major_minor = ".".join(minimum.split(".")[:2])
    assert f'"{major_minor}.' in workflow, (
        f"no {major_minor}.x entry in the test matrix for hacs.json minimum {minimum}"
    )


def test_translations_cover_every_string() -> None:
    """strings.json is the source; every translation must have the same keys."""
    reference = _flat_keys(json.loads((INTEGRATION / "strings.json").read_text()))
    assert reference, "strings.json is empty"

    translations = sorted((INTEGRATION / "translations").glob("*.json"))
    assert translations, "no translations at all"
    assert (INTEGRATION / "translations" / "en.json").is_file(), (
        "Home Assistant falls back to English, so en.json has to exist"
    )

    for path in translations:
        keys = _flat_keys(json.loads(path.read_text()))
        assert keys == reference, (
            f"{path.name} differs from strings.json: {reference ^ keys}"
        )


def test_config_flow_errors_are_translated() -> None:
    """Every error code the config flow can set needs a string."""
    flow = (INTEGRATION / "config_flow.py").read_text()
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    declared = set(strings.get("config", {}).get("error", {}))

    # The flow sets errors as errors[<field>] = "<code>".
    used = set()
    for line in flow.splitlines():
        if "errors[" in line and "] = " in line:
            used.add(line.split("] = ")[1].strip().strip('"'))

    assert used, "no error codes found in the config flow"
    assert used <= declared, f"untranslated error codes: {used - declared}"


# The rules hassfest applies to every translation value, transcribed from
# script/hassfest/translations.py so a red pipeline is not the first time we
# hear about a broken string.
_RE_URL = re.compile(
    r"(((ftp|ftps|scp|http|https|mqtt|mqtts|socket|socks5):\/\/|www\.)"
    r"[a-z0-9]+([\-\.]{1}[a-z0-9]+)*\.[a-z]{2,5}(:[0-9]{1,5})?(\/.*)?)",
    re.IGNORECASE,
)
_RE_PLACEHOLDER_IN_SINGLE_QUOTES = re.compile(r"'{\w+}'")
_RE_COMBINED_REFERENCE = re.compile(r"(.+\[%)|(%\].+)")


def _all_values(data: dict, prefix: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for key, value in data.items():
        if isinstance(value, dict):
            found.extend(_all_values(value, f"{prefix}{key}."))
        else:
            found.append((f"{prefix}{key}", value))
    return found


@pytest.mark.parametrize(
    "path",
    ["strings.json", "translations/en.json", "translations/de.json"],
)
def test_translation_values_pass_the_hassfest_rules(path: str) -> None:
    from homeassistant.helpers import config_validation as cv

    values = _all_values(json.loads((INTEGRATION / path).read_text()))
    assert values

    for key, value in values:
        assert isinstance(value, str), f"{key} is not a string"
        # No HTML, using Home Assistant's own validator.
        cv.string_with_no_html(value)
        assert not _RE_PLACEHOLDER_IN_SINGLE_QUOTES.search(value), key
        assert not _RE_COMBINED_REFERENCE.search(value), key
        assert value == value.strip(), f"{key} has leading or trailing spaces"
        assert not _RE_URL.search(value), f"{key} contains a URL: {value}"
        for _, field, _, _ in string.Formatter().parse(value):
            if field:
                assert field.isidentifier(), f"{key} has a bad placeholder: {field}"
