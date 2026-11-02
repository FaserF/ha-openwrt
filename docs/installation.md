# Installation

## Home Assistant Marketplace (formerly HACS) (Recommended)

This integration is fully compatible with the official **Home Assistant Marketplace** (as well as legacy HACS).

[![Open your Home Assistant instance and open the repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?repository=FaserF/ha-openwrt&category=integration)

> [!NOTE]
> This integration is available as a custom repository. The pull request for default inclusion is pending / approved.

1. Open **Marketplace** (or **HACS** on earlier versions) in Home Assistant.
2. Click on the three dots in the top right corner and select **Custom repositories**.
3. Add `FaserF/ha-openwrt` with category **Integration**.
4. Search for "OpenWrt".
5. Click **Download** / **Install** and restart Home Assistant.

## Manual Installation

1. Download the latest release from the [Releases page](https://github.com/FaserF/ha-openwrt/releases).
2. Extract the `custom_components/openwrt` folder into your Home Assistant's `custom_components` directory.
3. Restart Home Assistant.
