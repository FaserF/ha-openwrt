"""System data models for OpenWrt."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DeviceInfo:
    """OpenWrt device information."""

    hostname: str = ""
    model: str = ""
    board_name: str = ""
    firmware_version: str = ""
    kernel_version: str = ""
    architecture: str = ""
    target: str = ""
    mac_address: str = ""
    gateway_mac: str | None = None
    uptime: int = 0
    local_time: str = ""
    release_distribution: str = "OpenWrt"
    release_version: str = ""
    release_revision: str = ""


@dataclass
class StorageUsage:
    """Detailed storage usage information for a mount point."""

    mount_point: str = ""
    device: str = ""
    filesystem: str = ""
    total: int = 0
    used: int = 0
    free: int = 0
    percent: float = 0.0


@dataclass
class SystemResources:
    """System resource information."""

    cpu_usage: float = 0.0
    memory_total: int = 0
    memory_available: int = 0
    memory_available_percent: float = 0.0
    memory_used: int = 0
    memory_used_percent: float = 0.0
    memory_free: int = 0
    memory_buffered: int = 0
    memory_cached: int = 0
    swap_total: int = 0
    swap_used: int = 0
    swap_free: int = 0
    load_1min: float = 0.0
    load_5min: float = 0.0
    load_15min: float = 0.0
    uptime: int = 0
    processes: int = 0
    conntrack_count: int = 0
    conntrack_max: int = 0
    temperature: float | None = None
    temperatures: dict[str, float] = field(default_factory=dict)
    cpu_frequency: float | None = None
    filesystem_total: int = 0
    filesystem_used: int = 0
    filesystem_free: int = 0
    storage: list[StorageUsage] = field(default_factory=list)
    usb_devices: list[UsbDevice] = field(default_factory=list)
    top_processes: list[ProcessInfo] = field(default_factory=list)


@dataclass
class ProcessInfo:
    """Process information."""

    pid: int = 0
    user: str = ""
    command: str = ""
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    vsz: int = 0
    rss: int = 0


@dataclass
class UsbDevice:
    """USB device information."""

    id: str = ""  # Bus:Device e.g. 001:002
    vendor_id: str = ""
    product_id: str = ""
    manufacturer: str = ""
    product: str = ""
    serial: str = ""
    speed: str = ""  # e.g. 480M, 5G
    class_name: str = ""


@dataclass
class LedInfo:
    """Router LED information."""

    name: str = ""
    brightness: int = 0
    max_brightness: int = 255
    trigger: str = ""
    active: bool = False


@dataclass
class DiagnosticResult:
    """Result of a single diagnostic check."""

    name: str
    status: str  # "PASS", "FAIL", "WARN", "INFO"
    message: str
    details: str | None = None
    remedy: str | None = None
