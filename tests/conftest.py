"""Pytest configuration and fixtures for the OpenWrt integration tests."""

import sys
from unittest.mock import MagicMock

# Attempt to mock Home Assistant if it is not installed
# (e.g. running locally on Windows without C++ tools for lru-dict)
# Mock Home Assistant modules always to avoid collection errors in non-HA environments
def mock_submodule(name):
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

# Broadly mock HA submodules
ha_mocks = [
    "homeassistant",
    "homeassistant.core",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.config_validation",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.issue_registry",
    "homeassistant.helpers.typing",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.components",
    "homeassistant.components.binary_sensor",
    "homeassistant.components.button",
    "homeassistant.components.device_tracker",
    "homeassistant.components.diagnostics",
    "homeassistant.components.light",
    "homeassistant.components.repairs",
    "homeassistant.components.sensor",
    "homeassistant.components.switch",
    "homeassistant.components.update",
]

for mock_name in ha_mocks:
    mock_submodule(mock_name)

# Define specific exceptions used in code
class MockException(Exception):
    pass

sys.modules["homeassistant.exceptions"].ConfigEntryAuthFailed = MockException
sys.modules["homeassistant.exceptions"].ConfigEntryNotReady = MockException
sys.modules["homeassistant.exceptions"].HomeAssistantError = MockException

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
