from custom_components.openwrt.api.base import (
    MwanPolicy,
    MwanPolicyInterface,
    OpenWrtData,
)
from custom_components.openwrt.sensors.mwan import (
    _mwan_policy_attrs,
    _mwan_policy_status,
)


def test_mwan_policy_status_and_attrs() -> None:
    """Test policy status resolution and attributes generation."""
    policy1 = MwanPolicy(
        policy_name="wan_failover",
        family="ipv4",
        active_interfaces=[
            MwanPolicyInterface(interface="wan", percent=100),
            MwanPolicyInterface(interface="wanbackup", percent=0),
        ],
    )
    policy2 = MwanPolicy(
        policy_name="wan_failover",
        family="ipv6",
        active_interfaces=[
            MwanPolicyInterface(interface="wan", percent=50),
            MwanPolicyInterface(interface="wanbackup", percent=50),
        ],
    )
    policy3 = MwanPolicy(
        policy_name="offline",
        family="ipv4",
        active_interfaces=[],
    )
    data = OpenWrtData()
    data.mwan_policies = [policy1, policy2, policy3]

    # test status resolution
    assert _mwan_policy_status(data, "wan_failover", "ipv4") == "wan"
    assert _mwan_policy_status(data, "wan_failover", "ipv6") == "wan"
    assert _mwan_policy_status(data, "offline", "ipv4") == "none"

    # test attributes
    attrs1 = _mwan_policy_attrs(data, "wan_failover", "ipv4")
    assert attrs1["active_interfaces"] == 2
    assert attrs1["wan"] == "100%"
    assert attrs1["wanbackup"] == "0%"

    attrs2 = _mwan_policy_attrs(data, "offline", "ipv4")
    assert attrs2["active_interfaces"] == 0
