# mypy: disable-error-code="attr-defined"
from __future__ import annotations


class LuciRpcError(Exception):
    """Error communicating with LuCI RPC."""


class LuciRpcAuthError(LuciRpcError):
    """Authentication error."""


class LuciRpcTimeoutError(LuciRpcError):
    """Connection or request timeout."""


class LuciRpcConnectionError(LuciRpcError):
    """TCP connection failure (e.g. refused, unreachable)."""


class LuciRpcSslError(LuciRpcError):
    """SSL/TLS verification failure."""


class LuciRpcPackageMissingError(LuciRpcError):
    """Required package missing (e.g. 404 on /cgi-bin/luci/rpc)."""


__all__ = [
    "LuciRpcAuthError",
    "LuciRpcConnectionError",
    "LuciRpcError",
    "LuciRpcPackageMissingError",
    "LuciRpcSslError",
    "LuciRpcTimeoutError",
]
