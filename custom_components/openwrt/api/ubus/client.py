# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from ..base import (
    OpenWrtClient,
)
from .devices import UbusDevicesMixin
from .exceptions import *
from .features import UbusFeaturesMixin
from .network import UbusNetworkMixin
from .services import UbusServicesMixin
from .system import UbusSystemMixin
from .wireless import UbusWirelessMixin

_LOGGER = logging.getLogger(__name__)
UBUS_JSONRPC_VERSION = "2.0"
UBUS_ID_AUTH = 1
UBUS_ID_CALL = 2


class UbusClient(
    UbusSystemMixin,
    UbusNetworkMixin,
    UbusWirelessMixin,
    UbusDevicesMixin,
    UbusFeaturesMixin,
    UbusServicesMixin,
    OpenWrtClient,
):
    """Client for OpenWrt ubus JSON-RPC API."""

    def __init__(
        self,
        hass: Any,
        session: Any,
        host: str,
        username: str,
        password: str,
        port: int = 80,
        use_ssl: bool = False,
        verify_ssl: bool = False,
        ubus_path: str = "/ubus",
        dhcp_software: str = "auto",
        trust_stale_arp: bool = True,
        trust_bridge_fdb: bool = True,
    ) -> None:
        """Initialize the ubus client."""
        super().__init__(
            hass,
            session,
            host,
            username,
            password,
            port,
            use_ssl,
            verify_ssl,
            dhcp_software,
            trust_stale_arp,
            trust_bridge_fdb,
        )
        self._ubus_path = ubus_path
        self._session_id: str = "00000000000000000000000000000000"
        self._reauth_lock = asyncio.Lock()
        self._last_connect_time: float = 0.0
        # Set once we've checked whether the endpoint forces an http->https
        # redirect (e.g. uhttpd `redirect_https`); see _resolve_endpoint.
        self._endpoint_resolved: bool = False

        self._semaphore = asyncio.Semaphore(5)

    @property
    def _base_url(self) -> str:
        """Return base URL for ubus endpoint."""
        scheme = "https" if self.use_ssl else "http"
        return f"{scheme}://{self.host}:{self.port}{self._ubus_path}"

    def _build_request(
        self,
        method: str,
        params: list[Any] | dict[str, Any],
        request_id: int = UBUS_ID_CALL,
    ) -> dict[str, Any]:
        """Build a JSON-RPC request payload."""
        return {
            "jsonrpc": UBUS_JSONRPC_VERSION,
            "id": request_id,
            "method": method,
            "params": params,
        }

    async def _call(
        self,
        ubus_object: str,
        ubus_method: str,
        params: dict[str, Any] | None = None,
        reauthenticated: bool = False,
    ) -> dict[str, Any]:
        """Make a ubus call."""
        if self.session is None:
            raise UbusError("Session not initialized")
        session = self.session

        failed_session = self._session_id
        payload = self._build_request(
            "call",
            [failed_session, ubus_object, ubus_method, params or {}],
        )

        reauth_needed = False
        data: dict[str, Any] = {}
        try:
            async with self._semaphore:
                async with session.post(
                    self._base_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    ssl=self.verify_ssl if self.use_ssl else False,
                ) as response:
                    response.raise_for_status()
                    data = await response.json()

                    # Check for session expiration in ubus response
                    if (
                        data.get("result")
                        and isinstance(data["result"], list)
                        and len(data["result"]) > 0
                        and data["result"][0] == 6  # Permission denied/Session expired
                    ):
                        reauth_needed = True
                    elif (
                        isinstance(data.get("error"), dict)
                        and data["error"].get("code") == -32002
                    ):
                        # The ubus JSON-RPC gateway reports an invalid/expired session this way
                        # on some rpcd/uhttpd-mod-ubus versions, instead of embedding code 6 in
                        # a "result" array. Treat it the same as a session expiry.
                        reauth_needed = True

            if reauth_needed and not reauthenticated:
                if self._session_id == failed_session:
                    _LOGGER.debug("Ubus session expired, re-authenticating...")
                    await self.connect()
                return await self._call(
                    ubus_object,
                    ubus_method,
                    params,
                    reauthenticated=True,
                )

        except TimeoutError as err:
            msg = f"Timeout communicating with {self.host}"
            raise UbusTimeoutError(msg) from err
        except aiohttp.ClientConnectorError as err:
            msg = (
                f"Cannot connect to {self.host}. Is the IP correct and uhttpd running?"
            )
            raise UbusConnectionError(
                msg,
            ) from err
        except aiohttp.ClientSSLError as err:
            msg = f"SSL verification failed for {self.host}. Try disabling 'Verify SSL Certificate' if you use a self-signed one."
            raise UbusSslError(
                msg,
            ) from err
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                msg = f"Ubus endpoint not found on {self.host}. Is 'uhttpd-mod-ubus' installed?"
                raise UbusPackageMissingError(
                    msg,
                ) from err
            if err.status == 403:
                msg = f"Access denied to ubus on {self.host}. Check RPC permissions or switch to LuCI RPC."
                raise UbusPermissionError(
                    msg,
                ) from err
            msg = f"HTTP error {err.status} from {self.host}"
            raise UbusError(msg) from err
        except aiohttp.ClientError as err:
            if not reauthenticated:
                _LOGGER.debug(
                    "Ubus connection error (%s), retrying after session reset",
                    err,
                )
                await self.disconnect()
                return await self._call(
                    ubus_object,
                    ubus_method,
                    params,
                    reauthenticated=True,
                )
            self._connected = False
            msg = f"Communication error: {err}"
            raise UbusError(msg) from err
        except Exception as err:
            if isinstance(err, (UbusError, asyncio.CancelledError)):
                raise
            msg = f"Unexpected error communicating with {self.host}: {err}"
            raise UbusError(msg) from err

        if isinstance(data.get("error"), dict):
            err_obj = data["error"]
            code = err_obj.get("code")
            message = err_obj.get("message", "unknown error")
            if code == -32002:
                msg = (
                    f"Access denied to ubus on {self.host} (session invalid even "
                    "after re-authentication - check credentials/ACL)"
                )
                raise UbusPermissionError(msg)
            msg = f"Ubus JSON-RPC error {code} from {self.host}: {message}"
            raise UbusError(msg)

        if "result" not in data:
            msg = f"Unexpected response: {data}"
            raise UbusError(msg)

        result = data["result"]

        if isinstance(result, list):
            code = result[0] if result else -1
            if code == 6:  # Permission denied / Session expired
                # This should have been handled by the reauth logic above,
                # but if we get here, it's a real permission issue
                msg = f"Access denied to ubus on {self.host} (code 6)"
                raise UbusPermissionError(msg)
            if code == 0:
                return result[1] if len(result) > 1 else {}
            msg = f"Ubus call failed with code {code}"
            raise UbusError(msg)

        return result

    async def _list_objects(self) -> list[str]:
        """List all available ubus objects."""
        if self.session is None:
            raise UbusError("Session not initialized")
        session = self.session
        if not self._connected:
            await self.connect()

        token = self._session_id
        # The ubus JSON-RPC 'list' method requires [token, "*"] to return all
        # objects. Using only [token] returns an empty dict on most firmwares.
        payload = self._build_request(
            "list",
            [token, "*"],
            request_id=UBUS_ID_CALL,
        )

        try:
            async with session.post(
                self._base_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                ssl=self.verify_ssl if self.use_ssl else False,
            ) as response:
                response.raise_for_status()
                data = await response.json()
        except Exception as err:
            _LOGGER.debug("Failed to list ubus objects: %s", err)
            return []

        result = data.get("result")
        if not result or not isinstance(result, list):
            return []

        # On success, result is a list with one dict: [{"object1": {...}, "object2": {...}}]
        return list(result[0].keys())

    async def _get_object_methods(self, object_name: str) -> dict[str, Any]:
        """Get methods for a specific ubus object."""
        if self.session is None:
            raise UbusError("Session not initialized")
        session = self.session
        token = self._session_id
        payload = self._build_request(
            "list",
            [token, object_name],
            request_id=UBUS_ID_CALL,
        )
        try:
            async with session.post(
                self._base_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                ssl=self.verify_ssl if self.use_ssl else False,
            ) as response:
                response.raise_for_status()
                data = await response.json()
                result = data.get("result")
                if result and isinstance(result, list) and len(result) > 0:
                    return result[0].get(object_name, {})
        except Exception:
            pass
        return {}

    async def connect(self) -> bool:
        """Authenticate with ubus."""
        import time

        async with self._reauth_lock:
            if self._connected and (time.time() - self._last_connect_time < 5.0):
                _LOGGER.debug(
                    "Ubus already connected recently, skipping re-authentication"
                )
                return True
            try:
                res = await self._connect()
                if res:
                    self._last_connect_time = time.time()
                return res
            except Exception as err:
                self._last_connect_error = err
                raise

    async def _resolve_endpoint(self) -> None:
        """Upgrade to HTTPS if the ubus endpoint redirects there.

        Some hardened OpenWrt setups force HTTPS on uhttpd (``redirect_https``),
        often on a non-standard port, so a plain-http ubus call gets a 3xx to an
        ``https://`` URL. Following that redirect would work, but only after the
        JSON-RPC login (password included) has already crossed the wire in
        cleartext. Instead we probe once with a credential-free GET and, if
        redirected to https, switch scheme/port/path before authenticating.

        Runs at most once per client and is a no-op on stock http-only OpenWrt
        (no redirect) and on explicitly SSL-configured entries.
        """
        if self._endpoint_resolved or self.use_ssl or self.session is None:
            self._endpoint_resolved = True
            return
        try:
            async with self.session.get(
                self._base_url,
                allow_redirects=False,
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                location = resp.headers.get("Location", "")
                if resp.status in (301, 302, 307, 308) and location.startswith(
                    "https://"
                ):
                    parsed = urlsplit(location)
                    new_host = parsed.hostname
                    # urlsplit strips brackets off IPv6 literals; restore them
                    # so _base_url rebuilds a valid "https://[addr]:port/…".
                    if new_host and ":" in new_host:
                        new_host = f"[{new_host}]"
                    self.host = new_host or self.host
                    self.port = parsed.port or 443
                    if parsed.path:
                        self._ubus_path = parsed.path
                    self.use_ssl = True
                    _LOGGER.info(
                        "ubus endpoint redirects to HTTPS; upgraded to %s "
                        "(certificate verification stays disabled for the "
                        "common self-signed case unless you enable it)",
                        self._base_url,
                    )
        except Exception as err:  # noqa: BLE001
            # Non-fatal: fall back to the configured scheme and let the login
            # attempt surface any real connection error.
            _LOGGER.debug("ubus endpoint redirect probe failed: %s", err)
        self._endpoint_resolved = True

    async def _connect(self) -> bool:
        """Authenticate with ubus."""
        if self.session is None:
            raise UbusError("Session not initialized")
        await self._resolve_endpoint()
        session = self.session
        payload = self._build_request(
            "call",
            [
                "00000000000000000000000000000000",
                "session",
                "login",
                {"username": self.username, "password": self.password},
            ],
            request_id=UBUS_ID_AUTH,
        )

        try:
            async with session.post(
                self._base_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                ssl=self.verify_ssl if self.use_ssl else False,
            ) as response:
                response.raise_for_status()
                data = await response.json()
        except TimeoutError as err:
            msg = f"Login timeout for {self.host}"
            raise UbusTimeoutError(msg) from err
        except aiohttp.ClientConnectorError as err:
            msg = f"Cannot connect to {self.host}: {err}"
            raise UbusConnectionError(msg) from err
        except aiohttp.ClientSSLError as err:
            msg = f"SSL error connecting to {self.host}: {err}"
            raise UbusSslError(msg) from err
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                msg = f"Ubus endpoint not found on {self.host}. Is 'uhttpd-mod-ubus' installed?"
                raise UbusPackageMissingError(
                    msg,
                ) from err
            msg = f"HTTP error {err.status} during login: {err}"
            raise UbusError(msg) from err
        except aiohttp.ClientError as err:
            msg = f"Cannot connect to {self.host}: {err}"
            raise UbusError(msg) from err

        result = data.get("result")
        if (
            result is None
            or (isinstance(result, list) and not result)
            or (isinstance(result, list) and result[0] != 0)
        ):
            _LOGGER.error("Ubus auth failed: %s", data)
            msg = f"Authentication failed for {self.username}@{self.host}. Check credentials."
            raise UbusAuthError(
                msg,
            )

        if isinstance(result, list) and len(result) > 1:
            self._session_id = result[1].get("ubus_rpc_session", "")
        else:
            msg = "No session ID in auth response"
            raise UbusAuthError(msg)

        self._connected = True
        _LOGGER.debug(
            "Authenticated with %s, session: %s...",
            self.host,
            self._session_id[:8],
        )
        return True

    async def disconnect(self) -> None:
        """Log out from ubus and cleanup."""
        # Shared session managed by HA
        self._connected = False
