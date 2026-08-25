# mypy: disable-error-code="attr-defined"
from __future__ import annotations


class SshError(Exception):
    """Error communicating via SSH."""


class SshAuthError(SshError):
    """Authentication error."""


class SshTimeoutError(SshError):
    """Connection or request timeout."""


class SshConnectionError(SshError):
    """TCP connection failure (e.g. refused, unreachable)."""


class SshKeyError(SshError):
    """SSH key parsing or authentication failure."""


__all__ = [
    "SshAuthError",
    "SshConnectionError",
    "SshError",
    "SshKeyError",
    "SshTimeoutError",
]
