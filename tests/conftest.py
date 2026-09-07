"""Fixtures shared by the Swisspower DynPreis tests."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from .helpers import FETCH_TARGET, FakeApi


@pytest.fixture
def api() -> Iterator[FakeApi]:
    """Patch the API client for the whole test and hand back the fake."""
    fake = FakeApi()
    with patch(FETCH_TARGET, AsyncMock(side_effect=fake)):
        yield fake
