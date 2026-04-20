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
                parent_name = ".".join(parts[: i - 1])
                setattr(sys.modules[parent_name], parts[i - 1], mock)


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
    _attr_entity_registry_enabled_default: bool = True

    @property
    def entity_registry_enabled_default(self) -> bool:
        return self._attr_entity_registry_enabled_default

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
platforms = [
    "sensor",
    "binary_sensor",
    "switch",
    "button",
    "light",
    "update",
    "device_tracker",
    "event",
    "number",
]
for platform in platforms:
    module_name = f"homeassistant.components.{platform}"
    mock_module = MagicMock()

    # Generic Entity and Description classes for the platform
    ent_class_name = "".join([n.capitalize() for n in platform.split("_")]) + "Entity"
    desc_class_name = (
        "".join([n.capitalize() for n in platform.split("_")]) + "EntityDescription"
    )
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
sys.modules[
    "homeassistant.helpers.update_coordinator"
].CoordinatorEntity = MockCoordinatorEntity
sys.modules[
    "homeassistant.helpers.update_coordinator"
].DataUpdateCoordinator = MockDataUpdateCoordinator


class MockConfigFlow:
    """Mock ConfigFlow."""

    VERSION = 1

    def __init__(self, *args, **kwargs):
        self.hass = None
        self.context = {}
        self.unique_id: str | None = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    async def async_set_unique_id(self, unique_id, *, raise_on_progress=True):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self, *args, **kwargs):
        pass

    def async_show_form(self, *args, **kwargs):
        return {
            "type": "FORM",
            "step_id": kwargs.get("step_id"),
            "data_schema": kwargs.get("data_schema"),
            "errors": kwargs.get("errors"),
            "description_placeholders": kwargs.get("description_placeholders"),
        }

    def async_create_entry(self, *args, **kwargs):
        return {
            "type": "CREATE_ENTRY",
            "title": kwargs.get("title"),
            "data": kwargs.get("data"),
        }

    def async_abort(self, *args, **kwargs):
        return {"type": "ABORT", "reason": kwargs.get("reason")}


class MockOptionsFlow(MockConfigFlow):
    """Mock OptionsFlow."""


sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.config_entries"].ConfigFlow = MockConfigFlow
sys.modules["homeassistant.config_entries"].OptionsFlow = MockOptionsFlow
sys.modules["homeassistant.config_entries"].ConfigEntry = MagicMock()

const_mock = sys.modules["homeassistant.const"]
const_mock.CONF_HOST = "host"
const_mock.CONF_USERNAME = "username"
const_mock.CONF_PASSWORD = "password"
const_mock.CONF_PORT = "port"

ha_mocks = [
    "homeassistant.core",
    "homeassistant.config_entries",
    "homeassistant.helpers",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.config_validation",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.issue_registry",
    "homeassistant.helpers.service_info",
    "homeassistant.helpers.service_info.ssdp",
    "homeassistant.helpers.service_info.dhcp",
    "homeassistant.helpers.service_info.zeroconf",
    "homeassistant.helpers.typing",
    "homeassistant.components.diagnostics",
    "homeassistant.components.repairs",
    "homeassistant.util",
]

for mock_name in ha_mocks:
    mock_submodule(mock_name)

# Fix device_registry mocks to behave logically
dr_mock = MagicMock()
dr_mock.format_mac.side_effect = lambda x: x.lower() if isinstance(x, str) else x
sys.modules["homeassistant.helpers.device_registry"] = dr_mock
if "homeassistant.helpers" in sys.modules:
    sys.modules["homeassistant.helpers"].device_registry = dr_mock


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
    def __getattr__(self, name):
        return name


sys.modules["homeassistant.const"].UnitOfTime = MockEnum("UnitOfTime")
sys.modules["homeassistant.const"].PERCENTAGE = "%"
sys.modules["homeassistant.components.sensor"].SensorStateClass = MockEnum(
    "SensorStateClass",
)
sys.modules["homeassistant.components.sensor"].SensorDeviceClass = MockEnum(
    "SensorDeviceClass",
)
sys.modules[
    "homeassistant.components.binary_sensor"
].BinarySensorDeviceClass = MockEnum("BinarySensorDeviceClass")
sys.modules["homeassistant.components.update"].UpdateDeviceClass = MockEnum(
    "UpdateDeviceClass",
)
sys.modules["homeassistant.components.update"].UpdateEntityFeature = MockEnum(
    "UpdateEntityFeature",
)


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Override async_setup_entry."""
    with patch(
        "custom_components.openwrt.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        yield mock_setup_entry


@pytest.fixture
def mock_ubus_client() -> Generator[AsyncMock]:
    """Mock the Ubus API client."""
    with patch(
        "custom_components.openwrt.api.ubus.UbusClient",
        autospec=True,
    ) as mock_client:
        client = mock_client.return_value
        client.connect = AsyncMock()
        client.get_all_data = AsyncMock()
        client.get_all_data.return_value = AsyncMock()
        client.connected = True
        yield client


@pytest.fixture
def hass() -> MagicMock:
    """Mock HomeAssistant object."""
    mock_hass = MagicMock()
    mock_hass.config_entries = MagicMock()
    mock_hass.config_entries.flow = AsyncMock()
    mock_hass.data = {}
    mock_hass.services = MagicMock()
    mock_hass.services.has_service = MagicMock(return_value=False)
    mock_hass.services.async_register = MagicMock()
    return mock_hass
