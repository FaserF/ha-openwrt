# mypy: disable-error-code="attr-defined"
from __future__ import annotations


class UbusError(Exception):
    """Error communicating with ubus."""


class UbusAuthError(UbusError):
    """Authentication error."""


class UbusTimeoutError(UbusError):
    """Connection or request timeout."""


class UbusConnectionError(UbusError):
    """TCP connection failure (e.g. refused, unreachable)."""


class UbusSslError(UbusError):
    """SSL/TLS verification failure."""


class UbusPackageMissingError(UbusError):
    """Required package missing (e.g. 404 on /ubus)."""


class UbusPermissionError(UbusError):
    """Insufficient RPC permissions (e.g. 403 or ACL error). Consider switching to LuCI RPC for better accessibility."""


__all__ = [
    "UbusAuthError",
    "UbusConnectionError",
    "UbusError",
    "UbusPackageMissingError",
    "UbusPermissionError",
    "UbusSslError",
    "UbusTimeoutError",
]
