"""Pytest configuration and fixtures for the OpenWrt integration tests."""

import sys
from unittest.mock import MagicMock

# Attempt to mock Home Assistant if it is not installed
# (e.g. running locally on Windows without C++ tools for lru-dict)
try:
    import homeassistant  # noqa: F401
except ImportError:

    class MockHA:
        pass

    sys.modules["homeassistant"] = MagicMock()
    sys.modules["homeassistant.core"] = MagicMock()
    sys.modules["homeassistant.config_entries"] = MagicMock()
    sys.modules["homeassistant.exceptions"] = MagicMock()

    # Needs to match specific exceptions used in code
    class ConfigEntryAuthFailed(Exception):
        pass

    class ConfigEntryNotReady(Exception):
        pass

    sys.modules[
        "homeassistant.exceptions"
    ].ConfigEntryAuthFailed = ConfigEntryAuthFailed
    sys.modules["homeassistant.exceptions"].ConfigEntryNotReady = ConfigEntryNotReady

    sys.modules["homeassistant.helpers"] = MagicMock()
    sys.modules["homeassistant.helpers.device_registry"] = MagicMock()
    sys.modules["homeassistant.helpers.update_coordinator"] = MagicMock()

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Override async_setup_entry."""
    with patch(
        "custom_components.openwrt.async_setup_entry", return_value=True
    ) as mock_setup_entry:
        yield mock_setup_entry


@pytest.fixture
def mock_ubus_client() -> Generator[AsyncMock]:
    """Mock the Ubus API client."""
    with patch(
        "custom_components.openwrt.api.ubus.UbusClient", autospec=True
    ) as mock_client:
        client = mock_client.return_value
        client.connect = AsyncMock()
        client.get_all_data = AsyncMock()
        client.get_all_data.return_value = AsyncMock()
        client.connected = True
        yield client
