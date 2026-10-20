# Firmware Updates

The integration provides a powerful firmware update entity that supports official OpenWrt releases, Snapshot builds, and custom repositories.

## Functionality Matrix

| Feature | LuCI RPC | Ubus (uhttpd) | SSH | Custom Repo (GitHub) |
|---------|:---:|:---:|:---:|:---:|
| **Check for Official Version** | ✅ | ✅ | ✅ | ❌ (uses repo instead) |
| **Check for Snapshot Version** | ✅ | ✅ | ✅ | ✅ (via GitHub tags) |
| **Check for Custom Repo Version** | ❌ | ❌ | ❌ | ✅ |
| **Release Notes** | ✅ | ✅ | ✅ | ✅ (GitHub Release page) |
| **Install Official Release** | ✅ | ✅ | ✅ | ❌ |
| **Install from Custom Repo** | ✅ | ✅ | ✅ | ✅ (direct download) |
| **Attended Sysupgrade (ASU)** | ✅ | ✅ | ✅ | ❌ (not needed) |
| **Automated Backups** | ✅ | ✅ | ✅ | ✅ |
| **SHA256 Verification Before Flashing** | ✅ | ✅ | ✅ | ✅ (if the release has a `sha256sums` asset) |

## Attended Sysupgrade (ASU)

Attended Sysupgrade allows you to generate and install custom firmware images tailored to your specific router and installed packages directly from the UI.

- **Requirements**: The `luci-app-attendedsysupgrade` package must be installed on your router for LuCI/Ubus connections to enable the `Install` feature.
- **Custom Repositories**: If you use a custom OpenWrt fork (e.g. GL-iNet, FriendlyWrt), you can configure a custom ASU URL and repository pattern in the integration options.
- **Detection**: The integration reads the ASU server's `json/v1/overview.json` (branches, versions and the targets indexed so far) and the board's `json/v1/{path}/targets/{target}/{profile}.json`, the same data `owut` uses. The result is exposed as the `asu_status` attribute: `available`, `temporarily_unavailable`, `unsupported` or `error`, with the reason in `asu_status_reason`.
- **Temporarily unavailable**: Right after a release the ASU server can list the new branch before it has indexed any targets. Installation is then disabled until ASU catches up; the integration does not fall back to the plain image.

### Plain image safety

The plain sysupgrade image from downloads.openwrt.org only contains the default packages, so flashing it removes every package you added (VPNs, policy routing, multi-WAN, and so on). The integration therefore:

- never flashes the plain image on a device ASU supports, or will support once it has indexed the release: it installs through ASU or not at all;
- on devices ASU does not support, flashes the plain image only after confirming that every explicitly installed package (`/etc/apk/world`, or the packages opkg marks as user-installed) is part of the stock image for the running version. If any are not, or the check cannot be made, the install is refused and the message names the packages;
- refuses an ASU build if it cannot read the router's package list, because ASU would then build the stock package set.

When an install is refused, upgrade by hand with `owut` or the LuCI Attended Sysupgrade page.

### Automated Backups

For maximum safety, the integration can automatically trigger a router configuration backup before any firmware update starts.

- **Storage**: Backups are downloaded and stored locally in your Home Assistant configuration directory under `openwrt_backups/`.
- **Cleanup**: The remote backup file on the router is automatically removed after a successful download.
- **Toggle**: This feature is enabled by default but can be disabled in the integration options.
- **Retention Policy**: To prevent disk space exhaustion, you can configure a retention policy in the integration options. By default, backups older than **30 days** are automatically pruned when a new backup is created.

### Snapshot Logic

The integration automatically detects if your router is running a `SNAPSHOT` build.
- If **SNAPSHOT** is installed: It will search for newer snapshot builds periodically.
- If **Stable** is installed: It will only search for newer stable releases.

## Custom Firmware Repositories (GitHub Releases)

If you use a **custom OpenWrt fork** that publishes firmware via **GitHub Releases** (e.g. `AgustinLorenzo/openwrt`, or any other community build), you can configure this integration to check for updates from that repository — **without needing a custom ASU server**.

### How it works

1. **Configure**: In the integration options, set the **"Custom Firmware Repo"** field to the GitHub repository (e.g. `AgustinLorenzo/openwrt` or the full URL `https://github.com/AgustinLorenzo/openwrt`).
2. **Version Detection**: The integration queries the GitHub Releases API and compares the latest release tag against your router's current firmware revision hash (from `release.revision`).
3. **Asset Matching**: It automatically scans the release assets for a `sysupgrade.bin` file matching your router's **target** (e.g. `qualcommax-ipq807x`) and **board name** (e.g. `xiaomi_ax3600`). If a matching asset is found, the **Install** button is enabled.
4. **Checksum Verification**: If a `sha256sums` file is present in the release assets, the integration extracts the checksum for your firmware file, shows it, and checks the downloaded image against it on the router before running `sysupgrade`. On a mismatch the image is deleted and nothing is flashed. Without a `sha256sums` asset the image is flashed unverified, and a warning is logged.
5. **Release Notes**: The entity links directly to the GitHub release page for full release notes.

### Custom Firmware Pattern

For advanced users, the **"Custom Firmware Pattern"** option allows you to specify a custom regex pattern to match the correct sysupgrade binary in the release assets. This is useful if the repository uses non-standard naming conventions.

If left empty, the integration auto-generates a pattern based on your router's `target` and `board_name` — which works for most standard OpenWrt forks.

> [!NOTE]
> Custom firmware repositories use the **GitHub Releases API** (not ASU). This means:
> - **No custom ASU server is required** — the integration downloads directly from the GitHub release assets.
> - The repository must publish firmware as **GitHub Release assets** (`.bin` files attached to tagged releases).
> - Images are downloaded with TLS certificate verification and flashed without `sysupgrade -F`, so `sysupgrade` still rejects an image that does not match the board.
> - The GitHub API has rate limits (60 requests/hour for unauthenticated requests). Firmware checks happen periodically (every 6 hours by default), so this should not be an issue.

> [!WARNING]
> When a custom repo is configured, the integration will **only** check that repository for updates. Official OpenWrt release checks and ASU are disabled in this mode.
