"""Pytest configuration and fixtures for the OpenWrt integration tests."""

import sys
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Attempt to mock Home Assistant if it is not installed
# Mock Home Assistant modules always to avoid collection errors
def mock_submodule(name):
    """Recursively mock submodules to ensure they are available in sys.modules."""
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        full_name = ".".join(parts[:i])
        if full_name not in sys.modules:
            mock = MagicMock()
            sys.modules[full_name] = mock
            if i > 1:
                parent_name = ".".join(parts[:i-1])
                setattr(sys.modules[parent_name], parts[i-1], mock)

@dataclass(frozen=True, kw_only=True)
class MockEntityDescription:
    """Base class for mocked entity descriptions."""
    key: str
    name: str | None = None
    icon: str | None = None
    entity_category: Any | None = None
    entity_registry_enabled_default: bool = True
    translation_key: str | None = None
    translation_placeholders: dict[str, str] | None = None
    native_unit_of_measurement: str | None = None
    device_class: Any | None = None
    state_class: Any | None = None
    options: list[str] | None = None
    suggested_display_precision: int | None = None
    is_on_fn: Any | None = None
    available_fn: Any | None = None

class MockEntity:
    """Base class for mocked entities."""
    _attr_has_entity_name: bool = False
    _attr_unique_id: str | None = None
    _attr_name: str | None = None
    _attr_device_info: Any | None = None
    _attr_extra_state_attributes: dict[str, Any] | None = None
    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id
    @property
    def name(self) -> str | None:
        return self._attr_name
    def __init__(self, *args, **kwargs):
        pass
    def async_write_ha_state(self):
        pass
    async def async_update_ha_state(self, force_refresh=False):
        pass

class MockCoordinatorEntity(MockEntity):
    """Base class for mocked coordinator entities."""
    def __init__(self, coordinator, *args, **kwargs):
        self.coordinator = coordinator
        super().__init__(*args, **kwargs)
    @classmethod
    def __class_getitem__(cls, _):
        return cls

# Helper to ensure we return classes when accessed from MagicMock
def create_mock_class(base_class):
    mock = MagicMock()
    mock.__iter__ = None  # Prevent being treated as iterable
    return mock

# Pre-populate sys.modules with proper classes BEFORE any imports
platforms = ["sensor", "binary_sensor", "switch", "button", "light", "update", "device_tracker", "event", "number"]
for platform in platforms:
    module_name = f"homeassistant.components.{platform}"
    mock_module = MagicMock()

    # Generic Entity and Description classes for the platform
    ent_class_name = "".join([n.capitalize() for n in platform.split("_")]) + "Entity"
    desc_class_name = "".join([n.capitalize() for n in platform.split("_")]) + "EntityDescription"
    setattr(mock_module, ent_class_name, MockEntity)
    setattr(mock_module, desc_class_name, MockEntityDescription)

    # Specific common entity classes
    if platform == "device_tracker":
        mock_module.ScannerEntity = MockEntity
        mock_module.TrackerEntity = MockEntity
        mock_module.SourceType = MagicMock()

    sys.modules[module_name] = mock_module

# Other required classes
sys.modules["homeassistant.exceptions"] = MagicMock()
sys.modules["homeassistant.const"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.entity"] = MagicMock()
sys.modules["homeassistant.helpers.entity"].EntityDescription = MockEntityDescription
sys.modules["homeassistant.helpers.entity"].Entity = MockEntity
class MockDataUpdateCoordinator:
    def __init__(self, *args, **kwargs):
        self.data = None
        self.name = kwargs.get("name", "Unknown")
        self.config_entry = kwargs.get("config_entry")
        self.hass = args[0] if args else kwargs.get("hass")

    async def async_config_entry_first_refresh(self):
        pass

    def __class_getitem__(cls, _):
        return cls

sys.modules["homeassistant.helpers.update_coordinator"] = MagicMock()
sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = MockCoordinatorEntity
sys.modules["homeassistant.helpers.update_coordinator"].DataUpdateCoordinator = MockDataUpdateCoordinator

ha_mocks = [
    "homeassistant.core",
    "homeassistant.config_entries",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.config_validation",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.issue_registry",
    "homeassistant.helpers.typing",
    "homeassistant.components.diagnostics",
    "homeassistant.components.repairs",
    "homeassistant.util",
]

for mock_name in ha_mocks:
    mock_submodule(mock_name)

# Define specific exceptions
class MockException(Exception):
    pass


class UpdateFailed(MockException):
    pass

sys.modules["homeassistant.exceptions"].ConfigEntryAuthFailed = MockException
sys.modules["homeassistant.exceptions"].ConfigEntryNotReady = MockException
sys.modules["homeassistant.exceptions"].HomeAssistantError = MockException
sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed = UpdateFailed

# Constants and Enums
class MockEnum(str):
    def __getattr__(self, name): return name

sys.modules["homeassistant.const"].UnitOfTime = MockEnum("UnitOfTime")
sys.modules["homeassistant.const"].PERCENTAGE = "%"
sys.modules["homeassistant.components.sensor"].SensorStateClass = MockEnum("SensorStateClass")
sys.modules["homeassistant.components.sensor"].SensorDeviceClass = MockEnum("SensorDeviceClass")
sys.modules["homeassistant.components.binary_sensor"].BinarySensorDeviceClass = MockEnum("BinarySensorDeviceClass")
sys.modules["homeassistant.components.update"].UpdateDeviceClass = MockEnum("UpdateDeviceClass")
sys.modules["homeassistant.components.update"].UpdateEntityFeature = MockEnum("UpdateEntityFeature")

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
