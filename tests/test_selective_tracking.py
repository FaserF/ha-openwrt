"""Test selective device tracking in OpenWrt coordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.base import (
    ConnectedDevice,
    DhcpLease,
    OpenWrtData,
    SystemResources,
)
from custom_components.openwrt.const import (
    CONF_TRACKED_DEVICES,
    DATA_COORDINATOR,
    DOMAIN,
)
from custom_components.openwrt.coordinator import OpenWrtDataCoordinator


def _tracked_device_options(flow_result_selector) -> dict[str, str]:
    """Return the {value: label} mapping offered by the tracked devices selector."""
    options = flow_result_selector.call_args.kwargs["options"]
    return {opt["value"]: opt["label"] for opt in options}


@pytest.mark.asyncio
async def test_coordinator_selective_tracking() -> None:
    """Test that coordinator filters devices based on whitelist."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)

    config_entry = MagicMock()
    # Whitelist only device1
    config_entry.options = {CONF_TRACKED_DEVICES: ["00:bb:cc:dd:ee:01"]}
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()
    mock_client.connected = True

    # Mock data with two devices
    raw_data = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:01",
                hostname="device1",
                interface="br-lan",
                is_wireless=True,
            ),
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:02",
                hostname="device2",
                interface="br-lan",
                is_wireless=True,
            ),
        ],
        dhcp_leases=[
            DhcpLease(mac="00:bb:cc:dd:ee:01", hostname="device1", ip="192.168.1.10"),
            DhcpLease(mac="00:bb:cc:dd:ee:02", hostname="device2", ip="192.168.1.11"),
        ],
        network_interfaces=[],
        wireless_interfaces=[],
    )
    mock_client.get_all_data.return_value = raw_data

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    # Run update
    data = await coordinator._async_update_data()

    # Should only contain device1
    assert len(data.connected_devices) == 1
    assert data.connected_devices[0].mac == "00:bb:cc:dd:ee:01"

    assert len(data.dhcp_leases) == 1
    assert data.dhcp_leases[0].mac == "00:bb:cc:dd:ee:01"


@pytest.mark.asyncio
async def test_coordinator_no_whitelist() -> None:
    """Test that coordinator tracks all devices if no whitelist is configured."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)

    config_entry = MagicMock()
    config_entry.options = {}  # No whitelist
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()
    mock_client.connected = True

    raw_data = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:01",
                hostname="device1",
                interface="br-lan",
                is_wireless=True,
            ),
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:02",
                hostname="device2",
                interface="br-lan",
                is_wireless=True,
            ),
        ],
        dhcp_leases=[
            DhcpLease(mac="00:bb:cc:dd:ee:01", hostname="device1", ip="192.168.1.10"),
            DhcpLease(mac="00:bb:cc:dd:ee:02", hostname="device2", ip="192.168.1.11"),
        ],
        network_interfaces=[],
        wireless_interfaces=[],
    )
    mock_client.get_all_data.return_value = raw_data

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    # Run update
    data = await coordinator._async_update_data()

    # Should contain both devices
    assert len(data.connected_devices) == 2
    assert len(data.dhcp_leases) == 2


@pytest.mark.asyncio
async def test_coordinator_client_counts_ignore_whitelist() -> None:
    """Test that client counts reflect total occupancy, ignoring the whitelist."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)

    config_entry = MagicMock()
    # Whitelist only device1
    config_entry.options = {CONF_TRACKED_DEVICES: ["00:bb:cc:dd:ee:01"]}
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()
    mock_client.connected = True

    # Mock data with three devices:
    # 1. device1 (wireless, whitelisted)
    # 2. device2 (wireless, NOT whitelisted)
    # 3. device3 (wired, NOT whitelisted)
    raw_data = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:01",
                hostname="device1",
                interface="br-lan",
                is_wireless=True,
                connected=True,
            ),
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:02",
                hostname="device2",
                interface="br-lan",
                is_wireless=True,
                connected=True,
            ),
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:03",
                hostname="device3",
                interface="br-lan",
                is_wireless=False,
                connected=True,
            ),
        ],
        dhcp_leases=[],
        network_interfaces=[],
        wireless_interfaces=[],
    )
    mock_client.get_all_data.return_value = raw_data

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    # Run update
    data = await coordinator._async_update_data()

    # 1. Verify tracking (uses connected_devices)
    # Only device1 should be tracked because of the whitelist
    assert len(data.connected_devices) == 1
    assert data.connected_devices[0].mac == "00:bb:cc:dd:ee:01"

    # 2. Verify client counts (uses all_connected_devices)
    # All 3 devices should be in all_connected_devices regardless of whitelist
    assert len(data.all_connected_devices) == 3

    # Check the counts as they would be used by sensors
    connected_count = sum(1 for d in data.all_connected_devices if d.connected)
    wireless_count = sum(
        1 for d in data.all_connected_devices if d.is_wireless and d.connected
    )
    wired_count = sum(
        1 for d in data.all_connected_devices if not d.is_wireless and d.connected
    )

    assert connected_count == 3
    assert wireless_count == 2
    assert wired_count == 1


def _make_options_flow(all_connected_devices, device_history, tracked):
    """Build an options flow wired to a coordinator with the given state."""
    from custom_components.openwrt.config_flow import OpenWrtOptionsFlow

    config_entry = MagicMock()
    config_entry.entry_id = "test_entry"
    config_entry.options = {CONF_TRACKED_DEVICES: tracked}

    coordinator = MagicMock()
    coordinator.data = OpenWrtData(all_connected_devices=all_connected_devices)
    coordinator._device_history = device_history

    hass = MagicMock()
    hass.data = {DOMAIN: {config_entry.entry_id: {DATA_COORDINATOR: coordinator}}}

    flow = OpenWrtOptionsFlow(config_entry)
    flow.hass = hass
    return flow


@pytest.mark.asyncio
async def test_options_flow_offers_untracked_devices() -> None:
    """Untracked devices must be selectable in the options flow.

    Regression test: candidates used to come from _device_history alone, which
    only ever receives whitelisted devices. The candidate list then equalled the
    current selection, so the multi-select rendered with nothing to pick and the
    tracked set could never grow.
    """
    flow = _make_options_flow(
        all_connected_devices=[
            ConnectedDevice(mac="00:BB:CC:DD:EE:01", hostname="device1"),
            ConnectedDevice(mac="00:BB:CC:DD:EE:02", hostname="device2"),
            ConnectedDevice(mac="00:BB:CC:DD:EE:03", hostname="device3"),
        ],
        # Only the whitelisted device ever made it into history.
        device_history={
            "00:bb:cc:dd:ee:01": {"initially_seen": 1.0, "last_seen": 2.0},
        },
        tracked=["00:bb:cc:dd:ee:01"],
    )

    with patch("custom_components.openwrt.config_flow.selector") as mock_selector:
        result = await flow.async_step_options_select_devices()

    assert result["step_id"] == "options_select_devices"

    options = _tracked_device_options(mock_selector.SelectSelectorConfig)
    assert set(options) == {
        "00:bb:cc:dd:ee:01",
        "00:bb:cc:dd:ee:02",
        "00:bb:cc:dd:ee:03",
    }
    # The two untracked devices are what makes the dropdown usable at all.
    assert set(options) - {"00:bb:cc:dd:ee:01"}

    # Hostnames come from the live device list, not the MAC-only history.
    assert options["00:bb:cc:dd:ee:02"] == "device2 (00:BB:CC:DD:EE:02)"


@pytest.mark.asyncio
async def test_options_flow_keeps_offline_and_selected_devices() -> None:
    """Offline history devices and stored selections stay selectable."""
    flow = _make_options_flow(
        all_connected_devices=[
            ConnectedDevice(mac="00:bb:cc:dd:ee:01", hostname="device1"),
        ],
        device_history={
            "00:bb:cc:dd:ee:01": {"hostname": "device1"},
            # Seen before, currently offline.
            "00:bb:cc:dd:ee:09": {"hostname": "laptop"},
            # Defensive: stored history is not guaranteed to hold dicts.
            "00:bb:cc:dd:ee:99": "corrupt",
        },
        # Tracked but never seen by this coordinator yet.
        tracked=["00:bb:cc:dd:ee:01", "00:bb:cc:dd:ee:aa"],
    )

    with patch("custom_components.openwrt.config_flow.selector") as mock_selector:
        await flow.async_step_options_select_devices()

    options = _tracked_device_options(mock_selector.SelectSelectorConfig)

    # Offline device keeps its stored hostname.
    assert options["00:bb:cc:dd:ee:09"] == "laptop (00:bb:cc:dd:ee:09)"
    # Every stored selection remains a valid option.
    assert "00:bb:cc:dd:ee:aa" in options
    assert "00:bb:cc:dd:ee:99" not in options


@pytest.mark.asyncio
async def test_options_flow_without_coordinator_data() -> None:
    """A coordinator that has not completed a refresh must not break the form."""
    flow = _make_options_flow(
        all_connected_devices=[],
        device_history={"00:bb:cc:dd:ee:01": {"hostname": "device1"}},
        tracked=["00:bb:cc:dd:ee:01"],
    )
    flow.hass.data[DOMAIN]["test_entry"][DATA_COORDINATOR].data = None

    with patch("custom_components.openwrt.config_flow.selector") as mock_selector:
        result = await flow.async_step_options_select_devices()

    assert result["step_id"] == "options_select_devices"
    options = _tracked_device_options(mock_selector.SelectSelectorConfig)
    assert options == {"00:bb:cc:dd:ee:01": "device1 (00:bb:cc:dd:ee:01)"}


@pytest.mark.asyncio
async def test_device_history_records_hostname() -> None:
    """History must persist hostnames so offline devices stay readable."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)

    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()
    mock_client.connected = True
    mock_client.get_all_data.return_value = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:01",
                hostname="device1",
                interface="br-lan",
                is_wireless=True,
            ),
        ],
        dhcp_leases=[
            DhcpLease(mac="00:bb:cc:dd:ee:02", hostname="printer", ip="192.168.1.11"),
        ],
        network_interfaces=[],
        wireless_interfaces=[],
    )

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    await coordinator._async_update_data()

    assert coordinator._device_history["00:bb:cc:dd:ee:01"]["hostname"] == "device1"
    assert coordinator._device_history["00:bb:cc:dd:ee:02"]["hostname"] == "printer"


@pytest.mark.asyncio
async def test_device_history_ignores_placeholder_hostname() -> None:
    """dnsmasq's "*" placeholder must never be stored or shown as a hostname."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)

    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()
    mock_client.connected = True

    def _data(device_hostname: str, lease_hostname: str) -> OpenWrtData:
        return OpenWrtData(
            system_resources=SystemResources(uptime=100),
            connected_devices=[
                ConnectedDevice(
                    mac="00:bb:cc:dd:ee:01",
                    hostname=device_hostname,
                    interface="br-lan",
                    is_wireless=True,
                ),
            ],
            dhcp_leases=[
                DhcpLease(
                    mac="00:bb:cc:dd:ee:02",
                    hostname=lease_hostname,
                    ip="192.168.1.11",
                ),
            ],
            network_interfaces=[],
            wireless_interfaces=[],
        )

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    # First refresh: both report a real hostname.
    mock_client.get_all_data.return_value = _data("device1", "printer")
    await coordinator._async_update_data()

    assert coordinator._device_history["00:bb:cc:dd:ee:01"]["hostname"] == "device1"
    assert coordinator._device_history["00:bb:cc:dd:ee:02"]["hostname"] == "printer"

    # Later refresh reports "*": the known hostname must survive.
    # Reset the cached boot time first — with homeassistant.util.dt mocked out,
    # the drift comparison on a second refresh would operate on MagicMocks.
    coordinator._boot_time = None
    mock_client.get_all_data.return_value = _data("*", "*")
    await coordinator._async_update_data()

    assert coordinator._device_history["00:bb:cc:dd:ee:01"]["hostname"] == "device1"
    assert coordinator._device_history["00:bb:cc:dd:ee:02"]["hostname"] == "printer"


@pytest.mark.asyncio
async def test_device_history_never_stores_placeholder_hostname() -> None:
    """A device first seen as "*" is stored without a hostname, not with "*"."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)

    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()
    mock_client.connected = True
    mock_client.get_all_data.return_value = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:01",
                hostname="*",
                interface="br-lan",
                is_wireless=True,
            ),
        ],
        dhcp_leases=[
            DhcpLease(mac="00:bb:cc:dd:ee:02", hostname="*", ip="192.168.1.11"),
        ],
        network_interfaces=[],
        wireless_interfaces=[],
    )

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    await coordinator._async_update_data()

    assert coordinator._device_history["00:bb:cc:dd:ee:01"]["hostname"] == ""
    assert coordinator._device_history["00:bb:cc:dd:ee:02"]["hostname"] == ""


@pytest.mark.asyncio
async def test_options_flow_falls_back_to_mac_for_placeholder_hostname() -> None:
    """A "*" hostname must render as the MAC, not as a literal asterisk."""
    flow = _make_options_flow(
        all_connected_devices=[
            ConnectedDevice(mac="00:BB:CC:DD:EE:02", hostname="*"),
        ],
        device_history={},
        tracked=[],
    )

    with patch("custom_components.openwrt.config_flow.selector") as mock_selector:
        await flow.async_step_options_select_devices()

    options = _tracked_device_options(mock_selector.SelectSelectorConfig)
    assert options["00:bb:cc:dd:ee:02"] == "00:BB:CC:DD:EE:02 (00:BB:CC:DD:EE:02)"


@pytest.mark.asyncio
async def test_hostname_registry_shared_across_entries() -> None:
    """A router with DHCP data must publish hostnames for every device it sees.

    An access point runs no DHCP server, so its only route to a real name is the
    registry another entry populated. The registry therefore has to be filled
    before the whitelist filter, covering devices this entry does not track.
    """
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)
    hass.data = {}

    config_entry = MagicMock()
    # Only device1 is tracked here; device2's name must still be published.
    config_entry.options = {CONF_TRACKED_DEVICES: ["00:bb:cc:dd:ee:01"]}
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "router1"

    mock_client = AsyncMock()
    mock_client.connected = True
    mock_client.get_all_data.return_value = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:01",
                hostname="device1",
                interface="br-lan",
                is_wireless=True,
            ),
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:02",
                hostname="roaming-phone",
                interface="br-lan",
                is_wireless=True,
            ),
        ],
        dhcp_leases=[
            DhcpLease(mac="00:bb:cc:dd:ee:03", hostname="printer", ip="192.168.1.13"),
            # "*" placeholder must never be published as a name.
            DhcpLease(mac="00:bb:cc:dd:ee:04", hostname="*", ip="192.168.1.14"),
        ],
        network_interfaces=[],
        wireless_interfaces=[],
    )

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    await coordinator._async_update_data()

    registry = hass.data[DOMAIN]["hostname_registry"]
    # Untracked device: published anyway, which is the whole point.
    assert registry["00:bb:cc:dd:ee:02"] == "roaming-phone"
    assert registry["00:bb:cc:dd:ee:01"] == "device1"
    # Lease harvested before the whitelist dropped it.
    assert registry["00:bb:cc:dd:ee:03"] == "printer"
    assert "00:bb:cc:dd:ee:04" not in registry


@pytest.mark.asyncio
async def test_options_flow_uses_shared_hostnames() -> None:
    """An AP with no hostnames of its own labels devices from the registry."""
    flow = _make_options_flow(
        # An AP sees the association but has no name for it.
        all_connected_devices=[
            ConnectedDevice(mac="00:BB:CC:DD:EE:02", hostname=""),
        ],
        device_history={"00:bb:cc:dd:ee:09": {}},
        tracked=[],
    )
    flow.hass.data[DOMAIN]["hostname_registry"] = {
        "00:bb:cc:dd:ee:02": "roaming-phone",
        "00:bb:cc:dd:ee:09": "old-laptop",
    }

    with patch("custom_components.openwrt.config_flow.selector") as mock_selector:
        await flow.async_step_options_select_devices()

    options = _tracked_device_options(mock_selector.SelectSelectorConfig)
    assert options["00:bb:cc:dd:ee:02"] == "roaming-phone (00:BB:CC:DD:EE:02)"
    # History entries with no stored hostname benefit too.
    assert options["00:bb:cc:dd:ee:09"] == "old-laptop (00:bb:cc:dd:ee:09)"


@pytest.mark.asyncio
async def test_options_flow_prefers_local_hostname() -> None:
    """A hostname this router knows itself wins over the shared registry."""
    flow = _make_options_flow(
        all_connected_devices=[
            ConnectedDevice(mac="00:bb:cc:dd:ee:02", hostname="local-name"),
        ],
        device_history={},
        tracked=[],
    )
    flow.hass.data[DOMAIN]["hostname_registry"] = {"00:bb:cc:dd:ee:02": "shared-name"}

    with patch("custom_components.openwrt.config_flow.selector") as mock_selector:
        await flow.async_step_options_select_devices()

    options = _tracked_device_options(mock_selector.SelectSelectorConfig)
    assert options["00:bb:cc:dd:ee:02"].startswith("local-name")


@pytest.mark.asyncio
async def test_tracker_name_falls_back_to_shared_hostname() -> None:
    """An AP names its device from the registry, not the bare MAC.

    Regression: DeviceInfo uses `self.name or self._initial_name`, and `name`
    fell back to the MAC -- which is truthy -- so _initial_name was dead code
    and the device registry entry was named after the MAC.
    """
    from custom_components.openwrt.device_tracker import OpenWrtDeviceTracker

    mac = "00:bb:cc:dd:ee:20"
    coordinator = MagicMock()
    # The AP sees the association but resolved no hostname for it.
    coordinator.data.connected_devices = [ConnectedDevice(mac=mac, hostname="")]
    coordinator.data.dhcp_leases = []
    coordinator.data.device_info.hostname = "Router3"
    coordinator.hass.data = {
        DOMAIN: {"hostname_registry": {mac: "roaming-phone"}},
    }

    entry = MagicMock()
    entry.options = {}
    entry.data = {}
    entry.entry_id = "router3"
    tracker = OpenWrtDeviceTracker(coordinator, entry, mac, None)

    assert tracker.name == "roaming-phone"


@pytest.mark.asyncio
async def test_tracker_prefers_own_hostname_over_registry() -> None:
    """A hostname this entry resolved itself still wins."""
    from custom_components.openwrt.device_tracker import OpenWrtDeviceTracker

    mac = "00:bb:cc:dd:ee:20"
    coordinator = MagicMock()
    coordinator.data.connected_devices = [
        ConnectedDevice(mac=mac, hostname="roaming-phone.home")
    ]
    coordinator.data.dhcp_leases = []
    coordinator.data.device_info.hostname = "Router1"
    coordinator.hass.data = {DOMAIN: {"hostname_registry": {mac: "stale-name"}}}

    entry = MagicMock()
    entry.options = {}
    entry.data = {}
    entry.entry_id = "router1"
    tracker = OpenWrtDeviceTracker(coordinator, entry, mac, "roaming-phone.home")

    assert tracker.name == "roaming-phone.home"


@pytest.mark.asyncio
async def test_tracker_name_falls_back_to_mac_when_nothing_known() -> None:
    """With no name anywhere, the MAC remains the fallback."""
    from custom_components.openwrt.device_tracker import OpenWrtDeviceTracker

    mac = "00:bb:cc:dd:ee:21"
    coordinator = MagicMock()
    coordinator.data.connected_devices = [ConnectedDevice(mac=mac, hostname="")]
    coordinator.data.dhcp_leases = []
    coordinator.data.device_info.hostname = "Router3"
    coordinator.hass.data = {DOMAIN: {"hostname_registry": {}}}

    entry = MagicMock()
    entry.options = {}
    entry.data = {}
    entry.entry_id = "router3"
    tracker = OpenWrtDeviceTracker(coordinator, entry, mac, None)

    assert tracker.name == mac


@pytest.mark.asyncio
async def test_registry_seeded_from_persisted_history() -> None:
    """Stored hostnames populate the registry before the first poll completes."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)
    hass.data = {}

    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "router1"

    mock_client = AsyncMock()
    mock_client.connected = True
    mock_client.get_all_data.return_value = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[],
        dhcp_leases=[],
        network_interfaces=[],
        wireless_interfaces=[],
    )

    stored = {
        "00:bb:cc:dd:ee:20": {"hostname": "roaming-phone", "last_seen": 1.0},
        "00:11:22:33:44:55": {"hostname": "*", "last_seen": 1.0},
        "aa:bb:cc:dd:ee:ff": "corrupt",
    }

    def _make_store(_hass, _ver, key):
        # The per-entry history store and the shared hostname store are
        # distinct; only the former holds this payload.
        st = MagicMock()
        st.async_load = AsyncMock(
            return_value=None if key.endswith("_hostnames") else stored
        )
        st.async_save = AsyncMock()
        return st

    with patch(
        "custom_components.openwrt.coordinator.storage.Store", side_effect=_make_store
    ):
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    await coordinator._async_update_data()

    registry = hass.data[DOMAIN]["hostname_registry"]
    assert registry["00:bb:cc:dd:ee:20"] == "roaming-phone"
    assert "00:11:22:33:44:55" not in registry
    assert "aa:bb:cc:dd:ee:ff" not in registry


def test_resolve_client_name_prefers_local() -> None:
    """A hostname this entry resolved itself wins over the registry."""
    from custom_components.openwrt.helpers import resolve_client_name

    hass = MagicMock()
    hass.data = {DOMAIN: {"hostname_registry": {"00:bb:cc:dd:ee:20": "shared"}}}
    assert resolve_client_name(hass, "00:bb:cc:dd:ee:20", "Local-Name") == "Local-Name"


def test_resolve_client_name_uses_registry() -> None:
    """An AP with no name of its own borrows the DHCP router's."""
    from custom_components.openwrt.helpers import resolve_client_name

    hass = MagicMock()
    hass.data = {DOMAIN: {"hostname_registry": {"00:bb:cc:dd:ee:20": "roaming-phone"}}}
    mac = "00:bb:cc:dd:ee:20"
    # No local name at all, and the degenerate case where the "name" the
    # platform passed in is just the MAC again.
    assert resolve_client_name(hass, mac, None) == "roaming-phone"
    assert resolve_client_name(hass, mac, "") == "roaming-phone"
    assert resolve_client_name(hass, mac, mac) == "roaming-phone"
    assert resolve_client_name(hass, mac.upper(), mac.upper()) == "roaming-phone"
    assert resolve_client_name(hass, mac, "*") == "roaming-phone"


def test_resolve_client_name_falls_back_to_mac() -> None:
    """With nothing known anywhere the MAC remains the name."""
    from custom_components.openwrt.helpers import resolve_client_name

    hass = MagicMock()
    hass.data = {DOMAIN: {}}
    assert resolve_client_name(hass, "00:bb:cc:dd:ee:21", None) == "00:bb:cc:dd:ee:21"


@pytest.mark.asyncio
async def test_hostname_registry_persisted_and_reloaded() -> None:
    """The registry must survive a restart, available before any poll.

    Device records are named when entities are created during entry setup. An
    AP that starts before the DHCP router has polled would otherwise register
    its devices under bare MACs, and HA does not rewrite them afterwards.
    """
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)
    hass.data = {}

    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "router1"

    mock_client = AsyncMock()
    mock_client.connected = True
    mock_client.get_all_data.return_value = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:02",
                hostname="roaming-phone",
                interface="br-lan",
                is_wireless=True,
            ),
        ],
        dhcp_leases=[],
        network_interfaces=[],
        wireless_interfaces=[],
    )

    saved: dict[str, dict] = {}

    def _make_store(_hass, _ver, key):
        st = MagicMock()
        st.async_load = AsyncMock(return_value=saved.get(key))
        st.async_save = AsyncMock(
            side_effect=lambda d, k=key: saved.update({k: dict(d)})
        )
        return st

    with patch(
        "custom_components.openwrt.coordinator.storage.Store", side_effect=_make_store
    ):
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)
        await coordinator._async_update_data()

    # The shared store is keyed globally, not per entry.
    assert "openwrt_hostnames" in saved
    assert saved["openwrt_hostnames"]["00:bb:cc:dd:ee:02"] == "roaming-phone"

    # A second HA run: an AP starts up with no data of its own and must still
    # see the name before its first poll returns anything.
    hass2 = MagicMock()
    hass2.loop = MagicMock()
    hass2.loop.time = MagicMock(return_value=123456789.0)
    hass2.data = {}

    ap_entry = MagicMock()
    ap_entry.options = {}
    ap_entry.data = {"host": "192.168.1.3"}
    ap_entry.entry_id = "router3"

    ap_client = AsyncMock()
    ap_client.connected = True
    ap_client.get_all_data.return_value = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        # The AP sees the association but resolves no hostname.
        connected_devices=[
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:02",
                hostname="",
                interface="br-lan",
                is_wireless=True,
            ),
        ],
        dhcp_leases=[],
        network_interfaces=[],
        wireless_interfaces=[],
    )

    with patch(
        "custom_components.openwrt.coordinator.storage.Store", side_effect=_make_store
    ):
        ap_coord = OpenWrtDataCoordinator(hass2, ap_entry, ap_client)
        await ap_coord._async_load_hostname_registry()
        # Registry is populated before any device data has been processed.
        assert (
            hass2.data[DOMAIN]["hostname_registry"]["00:bb:cc:dd:ee:02"]
            == "roaming-phone"
        )


@pytest.mark.asyncio
async def test_hostname_registry_loaded_only_once() -> None:
    """A second entry must reuse the dict, not swap it out mid-flight."""
    hass = MagicMock()
    hass.data = {DOMAIN: {"hostname_registry": {"aa:bb:cc:dd:ee:ff": "existing"}}}

    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "router2"

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(
            return_value={"11:22:33:44:55:66": "should-not-load"}
        )
        mock_store.return_value.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, AsyncMock())
        original = hass.data[DOMAIN]["hostname_registry"]
        await coordinator._async_load_hostname_registry()

    assert hass.data[DOMAIN]["hostname_registry"] is original
    assert "11:22:33:44:55:66" not in hass.data[DOMAIN]["hostname_registry"]
