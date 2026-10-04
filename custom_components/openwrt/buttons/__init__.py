"""Button package for OpenWrt."""

from __future__ import annotations

from .base import (
    OpenWrtButtonDescription,
    OpenWrtButtonEntity,
    OpenWrtKickButton,
    OpenWrtWakeOnLanButton,
)
from .services import (
    BUTTONS,
    _add_device_buttons,
    _add_extra_service_buttons,
    _add_interface_buttons,
    _add_service_buttons,
    _add_static_buttons,
    _add_wireless_buttons,
    _get_unique_devices,
)

__all__ = [
    "BUTTONS",
    "OpenWrtButtonDescription",
    "OpenWrtButtonEntity",
    "OpenWrtKickButton",
    "OpenWrtWakeOnLanButton",
    "_add_device_buttons",
    "_add_extra_service_buttons",
    "_add_interface_buttons",
    "_add_service_buttons",
    "_add_static_buttons",
    "_add_wireless_buttons",
    "_get_unique_devices",
]
