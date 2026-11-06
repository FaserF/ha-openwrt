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
    assert attrs1["active_interfaces"] == 1
    assert attrs1["wan"] == "100%"
    assert attrs1["wanbackup"] == "0%"

    attrs2 = _mwan_policy_attrs(data, "offline", "ipv4")
    assert attrs2["active_interfaces"] == 0

    assert _mwan_policy_attrs(data, "wan_failover", "ipv6")["active_interfaces"] == 2


def test_mwan_policy_follows_the_member_carrying_the_traffic() -> None:
    """During a failover the backup is the only active interface."""
    data = OpenWrtData()
    data.mwan_policies = [
        MwanPolicy(
            policy_name="wan_failover",
            family="ipv4",
            active_interfaces=[
                MwanPolicyInterface(interface="wan", percent=0),
                MwanPolicyInterface(interface="wanbackup", percent=100),
            ],
        )
    ]

    assert _mwan_policy_status(data, "wan_failover", "ipv4") == "wanbackup"
    assert _mwan_policy_attrs(data, "wan_failover", "ipv4") == {
        "active_interfaces": 1,
        "wan": "0%",
        "wanbackup": "100%",
    }


def test_mwan_policy_with_only_idle_members_has_no_active_interface() -> None:
    """A policy whose members are all at 0 % routes over none of them.

    mwan3 keeps listing an offline member at 0 %, so a policy with a single
    interface must not keep naming it while that interface is down.
    """
    data = OpenWrtData()
    data.mwan_policies = [
        MwanPolicy(
            policy_name="wan_only",
            family="ipv4",
            active_interfaces=[MwanPolicyInterface(interface="wan", percent=0)],
        ),
        MwanPolicy(
            policy_name="wan_failover",
            family="ipv4",
            active_interfaces=[
                MwanPolicyInterface(interface="wan", percent=0),
                MwanPolicyInterface(interface="wanbackup", percent=0),
            ],
        ),
    ]

    assert _mwan_policy_status(data, "wan_only", "ipv4") == "none"
    assert _mwan_policy_status(data, "wan_failover", "ipv4") == "none"
    assert _mwan_policy_attrs(data, "wan_only", "ipv4") == {
        "active_interfaces": 0,
        "wan": "0%",
    }
    assert _mwan_policy_attrs(data, "wan_failover", "ipv4") == {
        "active_interfaces": 0,
        "wan": "0%",
        "wanbackup": "0%",
    }
