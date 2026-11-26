# Community Announcement Template

This document provides ready-to-post announcement copy for introducing the Home Assistant OpenWrt integration in community forums (such as the [OpenWrt Forum thread](https://forum.openwrt.org/t/home-assistant/220718/72) and Home Assistant Community).

## Forum Post Draft

**Title:** `[Home Assistant] OpenWrt Integration (ubus / LuCI RPC / SSH) with ASU Firmware Updates, Mesh & Presence Detection`

Hi everyone,

If you run OpenWrt routers or access points alongside Home Assistant, check out **[FaserF/ha-openwrt](https://github.com/FaserF/ha-openwrt)** — a custom integration for monitoring and managing OpenWrt devices directly from Home Assistant.

### Highlights

- **Flexible Connection Backends**: Works via native **ubus** (HTTP/HTTPS), **LuCI JSON-RPC**, or **SSH** (with automatic fallback options).
- **Rich Telemetry & Controls**: CPU/RAM/load sensors, interface & wireless status, connected Wi-Fi clients, WireGuard/OpenVPN peers, SQM/QoS metrics, PoE port controls, and router reboot/service actions.
- **Native Firmware Updates**: Integrates with Home Assistant's `update` entity, supporting Attended Sysupgrade (ASU), custom package preservation, and custom firmware repositories.
- **Multi-Router / AP Mesh Friendly**: Add main routers and dumb APs seamlessly with clean per-device entity organization.
- **HACS Ready**: Easy setup through HACS and UI Config Flow (YAML-free).

### Links

- **GitHub Repository**: <https://github.com/FaserF/ha-openwrt>
- **Documentation**: <https://faserf.github.io/ha-openwrt/>
- **Bug Reports & Feature Requests**: <https://github.com/FaserF/ha-openwrt/issues>

Feedback, router compatibility reports, and pull requests are very welcome!
