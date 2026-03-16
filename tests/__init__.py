"""Tests for the OpenWrt integration."""

import sys
from unittest.mock import MagicMock


class HAMock(MagicMock):
    @classmethod
    def __getattr__(cls, name: str) -> MagicMock:
        return MagicMock()


# Universal mock for homeassistant to bypass missing dependency locally
sys.modules["homeassistant"] = HAMock()
sys.modules["homeassistant.core"] = HAMock()
sys.modules["homeassistant.config_entries"] = HAMock()
sys.modules["homeassistant.exceptions"] = HAMock()
sys.modules["homeassistant.helpers"] = HAMock()
sys.modules["homeassistant.helpers.device_registry"] = HAMock()
sys.modules["homeassistant.helpers.update_coordinator"] = HAMock()
sys.modules["homeassistant.helpers.entity"] = HAMock()
sys.modules["homeassistant.components.update"] = HAMock()
sys.modules["homeassistant.components.sensor"] = HAMock()
sys.modules["homeassistant.components.binary_sensor"] = HAMock()
sys.modules["homeassistant.components.switch"] = HAMock()
sys.modules["homeassistant.components.button"] = HAMock()
sys.modules["homeassistant.components.device_tracker"] = HAMock()
sys.modules["homeassistant.components.repairs"] = HAMock()
sys.modules["homeassistant.components.diagnostics"] = HAMock()
sys.modules["homeassistant.const"] = HAMock()
sys.modules["homeassistant.data_entry_flow"] = HAMock()


class ParamikoMock(HAMock):
    pass


ParamikoMock.AuthenticationException = type("AuthenticationException", (Exception,), {})
ParamikoMock.SSHException = type("SSHException", (Exception,), {})
sys.modules["paramiko"] = ParamikoMock()


# Specific exceptions needed for inheritance
class ConfigEntryAuthFailed(Exception):
    pass


class ConfigEntryNotReady(Exception):
    pass


class UpdateFailed(Exception):
    pass


class HomeAssistantError(Exception):
    pass


sys.modules["homeassistant.exceptions"].ConfigEntryAuthFailed = ConfigEntryAuthFailed
sys.modules["homeassistant.exceptions"].ConfigEntryNotReady = ConfigEntryNotReady
sys.modules["homeassistant.exceptions"].HomeAssistantError = HomeAssistantError
sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed = UpdateFailed
