"""Test the OpenWrt device tracker platform."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.device_tracker import SourceType
from homeassistant.const import CONF_HOST
from homeassistant.helpers import (
    device_registry as dr,
)

from custom_components.openwrt.api.base import (
    ConnectedDevice,
    OpenWrtData,
    WirelessInterface,
)
from custom_components.openwrt.const import (
    CONF_CONSIDER_HOME,
    CONF_TRUST_STALE_ARP,
)
from custom_components.openwrt.device_tracker import OpenWrtDeviceTracker


@pytest.fixture
def mock_coordinator() -> MagicMock:
    """Mock coordinator."""
    coordinator = MagicMock()
    coordinator.data = OpenWrtData()
    coordinator.hass.data = {}
    return coordinator


@pytest.fixture
def mock_config_entry() -> MagicMock:
    """Mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.unique_id = "11:22:33:44:55:66"
    entry.data = {CONF_HOST: "192.168.1.1"}
    entry.options = {CONF_CONSIDER_HOME: 20}  # 20 seconds for testing
    return entry


def test_device_tracker_init(
    mock_coordinator: MagicMock, mock_config_entry: MagicMock
) -> None:
    """Test device tracker initialization."""
    mac = "AA:BB:CC:DD:EE:FF"
    tracker = OpenWrtDeviceTracker(mock_coordinator, mock_config_entry, mac)

    assert tracker.unique_id == f"openwrt_tracker_{mac.lower()}"
    assert tracker.mac_address == mac.lower()
    assert tracker.source_type == SourceType.ROUTER
    assert tracker._consider_home == timedelta(seconds=20)


def test_device_tracker_is_connected_logic(
    mock_coordinator: MagicMock, mock_config_entry: MagicMock
) -> None:
    """Test is_connected with consider_home logic."""
    mac = "aa:bb:cc:dd:ee:ff"
    tracker = OpenWrtDeviceTracker(mock_coordinator, mock_config_entry, mac)

    # 1. Initially not connected
    mock_coordinator.data.connected_devices = []
    assert tracker.is_connected is False

    # 2. Device appears
    # Populate shared state as coordinator would
    shared_data = mock_coordinator.hass.data.setdefault("openwrt", {})
    wireless_states = shared_data.setdefault("tracker_wireless_state", {})
    wireless_states[mac.lower()] = {
        "connected": True,
        "owner_entry_id": mock_config_entry.entry_id,
    }

    tracker._handle_coordinator_update()
    assert tracker.is_connected is True
    assert tracker._last_seen is not None
    last_seen_initial = tracker._last_seen

    # 3. Device disappears from global state
    wireless_states[mac.lower()]["connected"] = False
    tracker._handle_coordinator_update()
    assert tracker.is_connected is True

    # 4. Advance time but stay within 20s window
    with patch("custom_components.openwrt.device_tracker.datetime") as mock_datetime:
        now = last_seen_initial + timedelta(seconds=10)
        mock_datetime.now.return_value = now
        assert tracker.is_connected is True

        # 5. Advance time beyond 20s window
        now = last_seen_initial + timedelta(seconds=25)
        mock_datetime.now.return_value = now
        assert tracker.is_connected is False


def test_device_tracker_attributes(
    mock_coordinator: MagicMock, mock_config_entry: MagicMock
) -> None:
    """Test device tracker attributes."""
    mac = "aa:bb:cc:dd:ee:ff"
    tracker = OpenWrtDeviceTracker(mock_coordinator, mock_config_entry, mac)

    mock_coordinator.data.connected_devices = [
        ConnectedDevice(
            mac=mac.lower(),
            ip="192.168.1.100",
            hostname="my-phone",
            interface="br-lan",
            connected=True,
            connection_type="wired",
            neighbor_state="REACHABLE",
            uptime=3600,
        ),
    ]

    assert tracker.hostname == "my-phone"
    assert tracker.ip_address == "192.168.1.100"
    assert tracker.name == "my-phone"

    attrs = tracker.extra_state_attributes
    assert attrs["mac"] == mac.lower()
    assert attrs["connection_type"] == "wired"
    assert attrs["neighbor_state"] == "REACHABLE"
    assert attrs["interface"] == "br-lan"
    assert attrs["uptime"] == 3600


def test_device_tracker_stable_device_info(
    mock_coordinator: MagicMock, mock_config_entry: MagicMock
) -> None:
    """Test that device_info uses stable entry.unique_id (MAC)."""
    mac = "aa:bb:cc:dd:ee:ff"

    with patch(
        "custom_components.openwrt.device_tracker.DeviceInfo",
        side_effect=lambda **kwargs: kwargs,
    ):
        tracker = OpenWrtDeviceTracker(mock_coordinator, mock_config_entry, mac)

        # Change entry host IP
        mock_config_entry.data[CONF_HOST] = "192.168.1.200"

        device_info = tracker.device_info
        # via_device should be the router's stable unique_id (MAC), not the host IP
        # We check the second part of the tuple as DOMAIN might be mocked
        assert device_info["via_device"][1] == "11:22:33:44:55:66"
        assert (dr.CONNECTION_NETWORK_MAC, mac.lower()) in device_info["connections"]
        assert any(ident[1] == mac.lower() for ident in device_info["identifiers"])


def test_wireless_client_is_grouped_under_its_ssid(
    mock_coordinator: MagicMock, mock_config_entry: MagicMock
) -> None:
    """Resolve a wireless client's interface to its nested SSID device."""
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator.data = OpenWrtData(
        connected_devices=[
            ConnectedDevice(
                mac=mac,
                # OpenWrt may report the UCI section instead of the runtime
                # interface name for an associated wireless client.
                interface="default_radio0",
                is_wireless=True,
                connected=True,
            )
        ],
        wireless_interfaces=[
            WirelessInterface(
                name="phy0-ap0",
                section="default_radio0",
                ssid="Main",
                radio="radio0",
                band="2.4 GHz",
            )
        ],
    )
    mock_coordinator.interface_to_stable_id = {"phy0-ap0": "Main_2.4 GHz"}
    radio_device = MagicMock()
    registry = MagicMock()
    registry.async_get_device.return_value = radio_device

    with (
        patch(
            "homeassistant.helpers.device_registry.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.openwrt.device_tracker.DeviceInfo",
            side_effect=lambda **kwargs: kwargs,
        ),
    ):
        tracker = OpenWrtDeviceTracker(mock_coordinator, mock_config_entry, mac)
        device_info = tracker.device_info

    assert device_info["via_device"] == (
        "openwrt",
        "11:22:33:44:55:66_ap_Main_2.4 GHz",
    )
    registry.async_get_device.assert_called_with(
        identifiers={("openwrt", "11:22:33:44:55:66_ap_Main_2.4 GHz")}
    )


def test_device_tracker_randomized_mac(
    mock_coordinator: MagicMock, mock_config_entry: MagicMock
) -> None:
    """Test that randomized MACs are disabled by default."""
    # Normal MAC
    tracker_normal = OpenWrtDeviceTracker(
        mock_coordinator, mock_config_entry, "00:11:22:33:44:55"
    )
    assert (
        getattr(tracker_normal, "_attr_entity_registry_enabled_default", True) is True
    )

    # Randomized MAC (bit 1 of 1st byte is set: 02:...)
    tracker_random = OpenWrtDeviceTracker(
        mock_coordinator, mock_config_entry, "02:11:22:33:44:55"
    )
    assert tracker_random._attr_entity_registry_enabled_default is False

    # Another Randomized MAC (ae:...)
    tracker_random2 = OpenWrtDeviceTracker(
        mock_coordinator, mock_config_entry, "ae:11:22:33:44:55"
    )
    assert tracker_random2._attr_entity_registry_enabled_default is False


def test_device_tracker_multi_ap_attribution() -> None:
    """Test multi-AP last-wireless-writer wins attribution logic."""
    mac = "aa:bb:cc:dd:ee:ff"

    # Shared HA Data
    shared_data: dict[str, Any] = {}

    # Instance 1 (OG)
    coord1 = MagicMock()
    coord1.hass.data = shared_data
    coord1.config_entry.title = "AP-OG"
    coord1.data = OpenWrtData()
    entry1 = MagicMock()
    entry1.entry_id = "entry_og"
    entry1.options = {}
    entry1.data = {CONF_HOST: "192.168.1.2"}

    # Instance 2 (KG)
    coord2 = MagicMock()
    coord2.hass.data = shared_data
    coord2.config_entry.title = "AP-KG"
    coord2.data = OpenWrtData()
    entry2 = MagicMock()
    entry2.entry_id = "entry_kg"
    entry2.options = {}
    entry2.data = {CONF_HOST: "192.168.1.3"}

    tracker1 = OpenWrtDeviceTracker(coord1, entry1, mac)
    tracker2 = OpenWrtDeviceTracker(coord2, entry2, mac)

    # 1. Connected wirelessly to AP-OG
    coord1.data.connected_devices = [
        ConnectedDevice(
            mac=mac,
            connected=True,
            is_wireless=True,
            interface="phy0-ap0",
            signal=-30,
            connection_type="5GHz",
        ),
    ]
    # Simulate coordinator updating global state
    domain_data = shared_data.setdefault("openwrt", {})
    wireless_states = domain_data.setdefault("tracker_wireless_state", {})
    wireless_states[mac] = {
        "owner_entry_id": entry1.entry_id,
        "connected": True,
        "connected_ap": "AP-OG",
        "signal_strength": -30,
    }

    tracker1._handle_coordinator_update()

    assert tracker1.is_connected is True
    attrs1 = tracker1.extra_state_attributes
    assert attrs1["connected_ap"] == "AP-OG"
    assert attrs1["signal_strength"] == -30

    # Since tracker1 is the registered entity, check its state when queried by peers
    assert tracker2.is_connected is True
    assert tracker2.extra_state_attributes["connected_ap"] == "AP-OG"

    # 2. Roam to AP-KG
    coord1.data.connected_devices = []  # Disappears from OG
    tracker1._handle_coordinator_update()

    # Update shared state for AP-KG
    wireless_states[mac] = {
        "owner_entry_id": entry2.entry_id,
        "connected": True,
        "connected_ap": "AP-KG",
        "signal_strength": -45,
    }

    with patch.object(tracker1, "async_write_ha_state") as mock_write:
        # In the new architecture, the coordinator handles the peer notification.
        # We simulate this by calling the notification logic for the MAC.
        trackers = shared_data.get("openwrt", {}).get("all_trackers", {}).get(mac, [])
        for peer in trackers:
            peer.async_write_ha_state()

        mock_write.assert_called_once()

    assert tracker1.is_connected is True
    assert tracker1.extra_state_attributes["connected_ap"] == "AP-KG"
    assert tracker1.extra_state_attributes["signal_strength"] == -45


def test_device_tracker_stale_arp_presence(
    mock_coordinator: MagicMock, mock_config_entry: MagicMock
) -> None:
    """Test is_connected when a device is in STALE ARP state with trust_stale_arp setting."""
    mac = "10:e1:8e:51:5a:4a"
    tracker = OpenWrtDeviceTracker(mock_coordinator, mock_config_entry, mac)

    mock_coordinator.data.connected_devices = [
        ConnectedDevice(
            mac=mac,
            interface="br-lan",
            connected=True,
            is_wireless=False,
            neighbor_state="STALE",
        )
    ]

    # By default trust_stale_arp is True (or enabled)
    mock_config_entry.options = {CONF_TRUST_STALE_ARP: True}
    assert tracker.is_connected is True
    assert tracker._last_seen is not None
    last_seen = tracker._last_seen

    # When trust_stale_arp is disabled and consider_home window has passed
    mock_config_entry.options = {CONF_TRUST_STALE_ARP: False}
    with patch("custom_components.openwrt.device_tracker.datetime") as mock_datetime:
        mock_datetime.now.return_value = last_seen + timedelta(seconds=25)
        assert tracker.is_connected is False


@pytest.mark.asyncio
async def test_device_tracker_persists_offline_devices_from_history() -> None:
    """Test that device tracker creates entities for offline devices from history on setup."""
    from custom_components.openwrt.api.base import OpenWrtPermissions
    from custom_components.openwrt.const import DATA_COORDINATOR, DOMAIN
    from custom_components.openwrt.device_tracker import async_setup_entry

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {CONF_HOST: "192.168.1.1"}
    entry.options = {}

    coordinator = MagicMock()
    coordinator.hass = hass
    coordinator.config_entry = entry
    coordinator.data = OpenWrtData(
        connected_devices=[],
        dhcp_leases=[],
        permissions=OpenWrtPermissions(read_network=True, read_wireless=True),
    )
    coordinator._get_own_macs.return_value = set()
    coordinator._async_get_tracked_devices_whitelist.return_value = None
    coordinator._device_history = {
        "00:bb:cc:dd:ee:01": {
            "hostname": "offline-phone",
            "is_wireless": True,
            "initially_seen": 1700000000,
            "last_seen": 1700001000,
        }
    }

    hass.data = {DOMAIN: {entry.entry_id: {DATA_COORDINATOR: coordinator}}}
    mock_ent_reg = MagicMock()
    mock_ent_reg.entities = {}

    added_entities = []

    def mock_add_entities(entities: list) -> None:
        added_entities.extend(entities)

    with (
        patch(
            "custom_components.openwrt.device_tracker.er.async_get",
            return_value=mock_ent_reg,
        ),
        patch(
            "custom_components.openwrt.device_tracker.er.async_entries_for_config_entry",
            return_value=[],
        ),
    ):
        await async_setup_entry(hass, entry, mock_add_entities)

    assert len(added_entities) == 1
    tracker = added_entities[0]
    assert tracker.mac_address == "00:bb:cc:dd:ee:01"
    assert tracker.name == "offline-phone"
    assert tracker.is_connected is False
    attrs = tracker.extra_state_attributes
    assert attrs["is_wireless"] is True
    assert attrs["connection_type"] == "wireless"
    assert "last_seen" in attrs
    assert "initially_seen" in attrs


@pytest.mark.asyncio
async def test_device_tracker_fallback_to_dhcp_lease_when_offline() -> None:
    """Test that device tracker reads hostname/ip from dhcp_leases when not in connected_devices."""
    from custom_components.openwrt.api.base import DhcpLease

    coordinator = MagicMock()
    coordinator.data = OpenWrtData(
        connected_devices=[],
        dhcp_leases=[
            DhcpLease(
                mac="aa:bb:cc:dd:ee:02",
                hostname="tablet",
                ip="192.168.1.150",
            )
        ],
    )
    coordinator.hass.data = {}
    coordinator._device_history = {}

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    entry.data = {CONF_HOST: "192.168.1.1"}

    tracker = OpenWrtDeviceTracker(coordinator, entry, "aa:bb:cc:dd:ee:02")
    assert tracker.hostname == "tablet"
    assert tracker.ip_address == "192.168.1.150"
    assert tracker.name == "tablet"
    assert tracker.is_connected is False


@pytest.mark.asyncio
async def test_device_tracker_cleanup_unwhitelisted_entities() -> None:
    """Test that entities removed from whitelist are cleaned up."""
    from custom_components.openwrt.const import (
        CONF_TRACKED_DEVICES,
        DATA_COORDINATOR,
        DOMAIN,
    )
    from custom_components.openwrt.device_tracker import async_setup_entry

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {CONF_HOST: "192.168.1.1"}
    entry.options = {CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:01"]}

    coordinator = MagicMock()
    coordinator.hass = hass
    coordinator.config_entry = entry
    coordinator.data = OpenWrtData(
        connected_devices=[],
        dhcp_leases=[],
    )
    coordinator._get_own_macs.return_value = set()
    coordinator._async_get_tracked_devices_whitelist.return_value = {
        "aa:bb:cc:dd:ee:01"
    }
    coordinator._device_history = {
        "aa:bb:cc:dd:ee:01": {"hostname": "phone1", "is_wireless": True},
        "aa:bb:cc:dd:ee:02": {"hostname": "phone2", "is_wireless": True},
    }

    hass.data = {DOMAIN: {entry.entry_id: {DATA_COORDINATOR: coordinator}}}

    mock_ent_reg = MagicMock()
    mock_entry1 = MagicMock(
        domain="device_tracker",
        entity_id="device_tracker.phone1",
        unique_id="openwrt_tracker_aa:bb:cc:dd:ee:01",
    )
    mock_entry2 = MagicMock(
        domain="device_tracker",
        entity_id="device_tracker.phone2",
        unique_id="openwrt_tracker_aa:bb:cc:dd:ee:02",
    )

    job_callbacks = []
    hass.add_job = lambda cb: job_callbacks.append(cb)

    with (
        patch(
            "custom_components.openwrt.device_tracker.er.async_get",
            return_value=mock_ent_reg,
        ),
        patch(
            "custom_components.openwrt.device_tracker.er.async_entries_for_config_entry",
            return_value=[mock_entry1, mock_entry2],
        ),
    ):
        await async_setup_entry(hass, entry, MagicMock())
        # Run the scheduled cleanup job while patched
        for cb in job_callbacks:
            cb()

    # phone2 is not in whitelist, so its entity should be removed
    mock_ent_reg.async_remove.assert_called_once_with("device_tracker.phone2")
