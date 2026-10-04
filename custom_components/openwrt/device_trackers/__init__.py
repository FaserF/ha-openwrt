"""Device tracker package for OpenWrt."""

from __future__ import annotations

from .entity import OpenWrtDeviceTracker, compute_device_tracker_attrs

__all__ = [
    "OpenWrtDeviceTracker",
    "compute_device_tracker_attrs",
]
