"""Tests for SQM (Smart Queue Management) features."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.const import CONF_HOST
from custom_components.openwrt.const import DATA_CLIENT, DOMAIN
from custom_components.openwrt.api.base import OpenWrtData, SqmStatus
from custom_components.openwrt.sensor import _create_sqm_sensors
from custom_components.openwrt.switch import OpenWrtSqmSwitch
from custom_components.openwrt.number import OpenWrtSqmDownloadNumber, OpenWrtSqmUploadNumber


def test_sqm_status_model() -> None:
    """Test SQM status data model."""
    sqm = SqmStatus(
        section_id="eth0",
        name="eth0",
        enabled=True,
        interface="wan",
        download=100000,
        upload=50000,
        qdisc="fq_codel",
        script="simple.qos",
    )
    assert sqm.section_id == "eth0"
    assert sqm.name == "eth0"
    assert sqm.enabled is True
    assert sqm.interface == "wan"
    assert sqm.download == 100000
    assert sqm.upload == 50000
    assert sqm.qdisc == "fq_codel"
    assert sqm.script == "simple.qos"


def test_openwrt_data_sqm_field() -> None:
    """Test that OpenWrtData has the sqm field."""
    data = OpenWrtData()
    assert data.sqm == []
    
    sqm = SqmStatus(section_id="s1", name="n1")
    data = OpenWrtData(sqm=[sqm])
    assert len(data.sqm) == 1
    assert data.sqm[0].name == "n1"


@pytest.mark.asyncio
async def test_sqm_switch() -> None:
    """Test SQM switch entity."""
    sqm = SqmStatus(section_id="eth0", name="eth0", enabled=True)
    coordinator = MagicMock()
    coordinator.async_request_refresh = AsyncMock()
    coordinator.data = OpenWrtData(sqm=[sqm])
    client = AsyncMock()
    coordinator.client = client
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {CONF_HOST: "192.168.1.1"}
    
    entity = OpenWrtSqmSwitch(coordinator, entry, client, "eth0", "eth0")
    
    assert entity.name == "SQM eth0"
    assert entity.is_on is True
    assert entity.unique_id == "test_entry_sqm_eth0"
    
    # Test extra state attributes
    attrs = entity.extra_state_attributes
    assert attrs["interface"] == ""
    
    sqm.interface = "wan"
    assert entity.extra_state_attributes["interface"] == "wan"

    # Test turn on
    await entity.async_turn_on()
    client.set_sqm_config.assert_called_with("eth0", enabled=True)
    coordinator.async_request_refresh.assert_called()

    # Test turn off
    await entity.async_turn_off()
    client.set_sqm_config.assert_called_with("eth0", enabled=False)


@pytest.mark.asyncio
async def test_sqm_numbers() -> None:
    """Test SQM number entities."""
    sqm = SqmStatus(section_id="eth0", name="eth0", download=100, upload=50)
    coordinator = MagicMock()
    coordinator.async_request_refresh = AsyncMock()
    coordinator.data = OpenWrtData(sqm=[sqm])
    client = AsyncMock()
    coordinator.client = client
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {CONF_HOST: "192.168.1.1"}
    
    download_entity = OpenWrtSqmDownloadNumber(coordinator, entry, "eth0", "eth0")
    upload_entity = OpenWrtSqmUploadNumber(coordinator, entry, "eth0", "eth0")
    
    # Mock hass.data for set_native_value test
    hass = MagicMock()
    hass.data = {"openwrt": {"test_entry": {"client": client}}}
    download_entity.hass = hass
    upload_entity.hass = hass
    
    assert download_entity.native_value == 100
    assert upload_entity.native_value == 50
    
    # Test set value
    await download_entity.async_set_native_value(200)
    client.set_sqm_config.assert_called_with("eth0", download=200)
    
    await upload_entity.async_set_native_value(100)
    client.set_sqm_config.assert_called_with("eth0", upload=100)


def test_sqm_sensors() -> None:
    """Test SQM diagnostic sensors creation."""
    sqm = SqmStatus(
        section_id="eth0",
        name="eth0",
        interface="wan",
        qdisc="fq_codel",
        script="simple.qos",
    )
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(sqm=[sqm])
    
    entry = MagicMock()
    entry.entry_id = "test_entry"
    
    sensors = _create_sqm_sensors(coordinator, entry, "eth0", "eth0")
    assert len(sensors) == 3
    
    # Check names/keys
    keys = [s.entity_description.key for s in sensors]
    assert "sqm_eth0_interface" in keys
    assert "sqm_eth0_qdisc" in keys
    assert "sqm_eth0_script" in keys
    
    # Check values
    assert sensors[0].entity_description.value_fn(coordinator.data) == "wan"
    assert sensors[1].entity_description.value_fn(coordinator.data) == "fq_codel"
    assert sensors[2].entity_description.value_fn(coordinator.data) == "simple.qos"
