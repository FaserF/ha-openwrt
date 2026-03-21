from unittest.mock import patch

import pytest

from custom_components.openwrt.api.luci_rpc import LuciRpcClient
from custom_components.openwrt.api.ssh import SshClient
from custom_components.openwrt.api.ubus import UbusClient

DF_OUTPUT = """Filesystem           1K-blocks      Used Available Use% Mounted on
/dev/root                 2048      2048         0 100% /rom
tmpfs                   124152      1232    122920   1% /tmp
/dev/mtdblock3           10240      1500      8740  15% /overlay
overlayfs:/overlay       10240      1500      8740  15% /
/dev/sda1             61440000  10240000  51200000  17% /mnt/usb
"""


@pytest.mark.asyncio
async def test_ubus_storage_monitoring():
    client = UbusClient("192.168.1.1", "root", "pass")

    # Mock _call for df -Pk (file.exec) - ubus returns a dict
    with patch.object(client, "_call", return_value={"stdout": DF_OUTPUT}):
        resources = await client.get_system_resources()

        assert len(resources.storage) == 5
        usb = next(s for s in resources.storage if s.mount_point == "/mnt/usb")
        assert usb.total == 61440000 * 1024
        assert usb.percent == 17.0
        assert usb.device == "/dev/sda1"

        # Legacy check (should prefer /overlay or first /)
        assert resources.filesystem_total == 10240 * 1024
        assert resources.filesystem_used == 1500 * 1024


@pytest.mark.asyncio
async def test_ssh_storage_monitoring():
    client = SshClient("192.168.1.1", "root", "pass")

    # Mock _exec for the 5 commands in gather
    with patch.object(
        client,
        "_exec",
        side_effect=[
            "MemTotal: 256000 kB\nMemFree: 100000 kB",  # meminfo
            "0.10 0.05 0.01",  # loadavg
            "12345.67 89012.34",  # uptime
            "cpu 1 2 3 4 5 6 7 8",  # stat
            DF_OUTPUT,  # df -Pk
        ],
    ):
        resources = await client.get_system_resources()

        assert len(resources.storage) == 5
        usb = next(s for s in resources.storage if s.mount_point == "/mnt/usb")
        assert usb.device == "/dev/sda1"
        assert usb.free == 51200000 * 1024
        assert usb.mount_point == "/mnt/usb"


@pytest.mark.asyncio
async def test_luci_rpc_storage_monitoring():
    client = LuciRpcClient("192.168.1.1", "root", "pass")

    # luci_rpc uses _rpc_call with index 4 for df in the gather list
    # cmds = [meminfo, loadavg, uptime, stat, df, sysinfo, mounts]
    results = [
        "MemTotal: 256000 kB",  # 0
        "0.10 0.05 0.01",  # 1
        "12345.67",  # 2
        "cpu 1 2 3 4 5 6 7 8",  # 3
        DF_OUTPUT,  # 4 (df)
        "{}",  # 5 (sysinfo)
        "{}",  # 6 (mounts)
    ]

    with patch.object(client, "_rpc_call", side_effect=results):
        resources = await client.get_system_resources()

        assert len(resources.storage) == 5
        root = next(s for s in resources.storage if s.mount_point == "/")
        assert root.total == 10240 * 1024
        assert root.percent == 15.0
