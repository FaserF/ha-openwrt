# mypy: disable-error-code="attr-defined,has-type,assignment"
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME

from ..api.ssh import (
    SshClient,
)
from ..const import (
    CONF_CONNECTION_TYPE,
    CONNECTION_TYPE_LUCI_RPC,
    CONNECTION_TYPE_SSH,
    CONNECTION_TYPE_UBUS,
    DEFAULT_PORT_SSH,
    DOMAIN,
)
from .helpers import selector, translation


def create_client(*args: Any, **kwargs: Any) -> Any:
    from .. import config_flow

    return config_flow.create_client(*args, **kwargs)


_LOGGER = logging.getLogger(__name__)

CONNECTION_TYPE_MAP = {
    CONNECTION_TYPE_UBUS: "ubus (HTTP/HTTPS)",
    CONNECTION_TYPE_LUCI_RPC: "LuCI RPC",
    CONNECTION_TYPE_SSH: "SSH (not recommended)",
}


class ProvisioningFlowMixin:
    """Dedicated Home Assistant user provisioning and repair steps."""

    async def async_step_provision_user(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Step to ask if user wants to provision a dedicated user."""
        _LOGGER.info(
            "Entering async_step_provision_user: input=%s, errors=%s",
            user_input,
            errors,
        )
        if user_input is not None:
            mode = user_input.get("mode")
            if mode in {"create", "reset"}:
                return await self.async_step_do_provision()
            if mode == "reuse":
                return await self.async_step_reuse_user()
            return await self.async_step_permissions()

        options = ["create", "skip"]
        default_mode = "create"
        user_exists_info = ""

        if self._homeassistant_user_exists:
            options = ["reuse", "reset", "skip"]
            default_mode = "reuse"
            user_exists_info = "An existing **homeassistant** user was detected on your router. You can either reuse it or reset it with a new password and freshly generated permissions."

        return self.async_show_form(
            step_id="provision_user",
            data_schema=vol.Schema(
                {
                    vol.Required("mode", default=default_mode): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            translation_key="provision_mode",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        ),
                    ),
                },
            ),
            errors=errors or {},
            description_placeholders={
                "security_link": "https://github.com/FaserF/ha-openwrt/blob/main/SECURITY.md",
                "user_exists_info": user_exists_info,
            },
        )

    async def async_step_reuse_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step to ask for existing user password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            test_data = self._data.copy()
            test_data[CONF_USERNAME] = "homeassistant"
            test_data[CONF_PASSWORD] = user_input[CONF_PASSWORD]

            error = await self._test_connection(test_data)
            if not error:
                self._data.update(test_data)
                return await self.async_step_permissions()
            errors["base"] = error

        return self.async_show_form(
            step_id="reuse_user",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    def _analyze_provision_error(self, error_text: str) -> str:
        """Analyze the provisioning error and return a user-friendly tip."""
        if "ANALYSIS: PASSWD_WRITE_FAILED" in error_text:
            return "The router's /etc/passwd file is read-only or the disk is full. This is common on some specialized firmwares or if the Overlay partition is not mounted."
        if (
            "ANALYSIS: CHPASSWD_FAILED" in error_text
            or "ANALYSIS: PASSWD_BINARY_FAILED" in error_text
        ):
            return "The password update failed. This can happen if the router uses a specialized authentication backend or if the password contains unsupported characters."
        if "ANALYSIS: ACL_WRITE_FAILED" in error_text:
            return "Could not create the ACL file at /usr/share/rpcd/acl.d/. This usually means the partition is read-only (standard for SquashFS builds without an Overlay)."
        if "ANALYSIS: UCI_COMMIT_FAILED" in error_text:
            return "Failed to save configuration changes via UCI. Check if another process is locking the UCI database."
        if not error_text or error_text.strip() == "":
            return "The router returned an empty response. This often means the 'file.exec' command was blocked by the router's security policy (common on Xiaomi/OEM firmwares)."
        if "permission denied" in error_text.lower():
            return "Permission denied. Ensure you are connecting as 'root' or that your current user has 'file.exec' permissions."

        return "An unexpected error occurred during execution. See the logs below for details."

    async def async_step_do_provision(self) -> ConfigFlowResult:
        """Perform the actual provisioning."""
        self._generated_password = secrets.token_hex(16)
        client = create_client(self.hass, self._data)
        success = False
        self._provision_error = None

        try:
            async with asyncio.timeout(45):
                await client.connect()
                # The provisioning script will restart services in background
                # it's possible the connection drops exactly when/after sending SUCCESS
                success, self._provision_error = await client.provision_user(
                    "homeassistant",
                    self._generated_password,
                )

                # Fallback to SSH for root if ubus provisioning fails
                # (e.g. Xiaomi/OEM routers block file.exec via ubus)
                if (
                    not success
                    and self._data.get(CONF_USERNAME) == "root"
                    and self._data.get(CONF_CONNECTION_TYPE) != CONNECTION_TYPE_SSH
                ):
                    _LOGGER.info(
                        "Provisioning via %s failed for root, trying SSH fallback on port 22",
                        self._data.get(CONF_CONNECTION_TYPE),
                    )

                    # Always use port 22 for SSH fallback, independent of the ubus port
                    ssh_config = dict(self._data)
                    ssh_config[CONF_CONNECTION_TYPE] = CONNECTION_TYPE_SSH
                    ssh_config[CONF_USERNAME] = "root"
                    ssh_config[CONF_PORT] = DEFAULT_PORT_SSH
                    ssh_client = create_client(self.hass, ssh_config)
                    try:
                        if await ssh_client.connect():
                            success, ssh_error = await ssh_client.provision_user(
                                "homeassistant",
                                self._generated_password,
                            )
                            if success:
                                self._provision_error = None
                                _LOGGER.info(
                                    "SSH fallback provisioning succeeded for %s",
                                    self._data.get(CONF_HOST),
                                )
                            else:
                                self._provision_error = (
                                    f"ubus file.exec is blocked on this router (common on Xiaomi/OEM firmwares). "
                                    f"SSH fallback also failed: {ssh_error}. "
                                    "Please reconfigure using SSH as the connection type."
                                )
                        else:
                            self._provision_error = (
                                "ubus file.exec is blocked on this router (common on Xiaomi/OEM firmwares) "
                                "and SSH on port 22 is not reachable either. "
                                "Please enable SSH on the router (System → Administration → SSH Access) "
                                "and reconfigure using SSH as the connection type."
                            )
                    except Exception as err:
                        _LOGGER.debug("SSH fallback failed: %s", err)
                        self._provision_error = (
                            f"ubus file.exec is blocked on this router (common on Xiaomi/OEM firmwares). "
                            f"SSH fallback error: {err}. "
                            "Please enable SSH (System → Administration → SSH Access) "
                            "and reconfigure using SSH as the connection type."
                        )
                    finally:
                        await ssh_client.disconnect()

        except TimeoutError:
            _LOGGER.warning(
                "Provisioning timed out for %s. It might have succeeded if services are restarting.",
                self._data.get(CONF_HOST),
            )
            self._provision_error = "Timeout during provisioning. The router might be slow or restarting services."
        except Exception as err:
            err_msg = str(err).lower()
            if any(
                m in err_msg
                for m in ["connection reset", "broken pipe", "closed", "eof"]
            ):
                _LOGGER.info(
                    "Connection dropped during provisioning for %s - this is expected during service restarts.",
                    self._data.get(CONF_HOST),
                )
                success = True
            else:
                _LOGGER.exception(
                    "Provisioning failed for %s: %s",
                    self._data.get(CONF_HOST),
                    err,
                )
                self._provision_error = str(err)
        finally:
            await client.disconnect()

        if success:
            # Wait for rpcd to fully restart and apply ACLs
            # We already changed the script to background restart with sleep,
            # but we wait here too for a good first attempt in the next step
            await asyncio.sleep(5)
            return await self.async_step_display_new_user()

        return await self.async_step_provision_failed()

    async def async_step_provision_failed(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle provisioning failure step."""
        if user_input is not None:
            return await self.async_step_permissions()

        tip = self._analyze_provision_error(self._provision_error or "")
        return self.async_show_form(
            step_id="provision_failed",
            errors={"base": "provision_failed"},
            description_placeholders={
                "error": self._provision_error or "Unknown error",
                "tip": tip,
            },
        )

    async def async_step_diagnostics(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show diagnostic report after failure."""
        if user_input is not None:
            if user_input.get("run_repair"):
                if self._data.get(CONF_USERNAME) == "root":
                    self._repair_username = "root"
                    self._repair_password = self._data.get(CONF_PASSWORD, "")
                    self._repair_port = 22
                    return await self.async_step_run_repair()
                return await self.async_step_repair_credentials()
            return await self.async_step_credentials()

        # Get translations for footer elements
        translations = await translation.async_get_translations(
            self.hass, self.hass.config.language, "config", [DOMAIN]
        )

        def t(key: str, default: str) -> str:
            full_key = f"component.openwrt.entity.sensor.config_flow_ui.state.{key}"
            if translations and full_key in translations:
                return translations[full_key]
            return default

        schema = vol.Schema(
            {
                vol.Optional("run_repair", default=False): bool,
            }
        )

        return self.async_show_form(
            step_id="diagnostics",
            data_schema=schema,
            description_placeholders={
                "report": self._diagnostic_report or "No diagnostic data available.",
                "footer_title": t("diagnostics_footer_title", "How to use this report"),
                "footer_intro": t(
                    "diagnostics_footer_intro",
                    "If you need help, copy this report and share it on the",
                ),
                "footer_link_text": t(
                    "diagnostics_footer_link_text", "GitHub issues page"
                ),
                "issues_url": "https://github.com/FaserF/ha-openwrt/issues",
            },
        )

    async def async_step_repair_credentials(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Prompt for SSH root credentials to perform repair."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._repair_username = user_input.get(CONF_USERNAME, "root")
            self._repair_password = user_input.get(CONF_PASSWORD, "")
            self._repair_port = user_input.get(CONF_PORT, 22)
            return await self.async_step_run_repair()

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default="root"): str,
                vol.Optional(CONF_PASSWORD, default=""): str,
                vol.Required(CONF_PORT, default=22): int,
            }
        )
        return self.async_show_form(
            step_id="repair_credentials",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_run_repair(self) -> ConfigFlowResult:
        """Connect via SSH and perform the repair."""
        host = self._data.get(CONF_HOST) or ""
        conn_type = self._data.get(CONF_CONNECTION_TYPE, CONNECTION_TYPE_UBUS)

        try:
            # We connect using SshClient
            ssh = SshClient(
                hass=self.hass,
                session=None,
                host=host,
                username=self._repair_username,
                password=self._repair_password,
                port=self._repair_port,
            )
            connected = await ssh.connect()
            if not connected:
                raise Exception("SSH connection failed to connect.")

            # Let's perform repair commands
            # 1. Start web server uhttpd
            await ssh.execute_command("/etc/init.d/uhttpd start 2>/dev/null || true")

            # 2. Check if we need to install packages
            # Let's check which package manager is present
            pkg_mgr = "opkg"
            apk_check = await ssh.execute_command("command -v apk 2>/dev/null || true")
            if apk_check and "apk" in apk_check:
                pkg_mgr = "apk"

            # Determine if we should install luci-mod-rpc or uhttpd-mod-ubus
            report_str = str(self._diagnostic_report or "").lower()
            needs_luci_rpc = conn_type == CONNECTION_TYPE_LUCI_RPC and (
                "404" in report_str
                or "rpc endpoint" in report_str
                or "session" in report_str
            )
            needs_ubus = conn_type == CONNECTION_TYPE_UBUS and (
                "404" in report_str or "rpc endpoint" in report_str
            )

            if needs_luci_rpc:
                if pkg_mgr == "apk":
                    await ssh.execute_command("apk update && apk add luci-mod-rpc")
                else:
                    await ssh.execute_command(
                        "opkg update && opkg install luci-mod-rpc"
                    )
            elif needs_ubus:
                if pkg_mgr == "apk":
                    await ssh.execute_command("apk update && apk add uhttpd-mod-ubus")
                else:
                    await ssh.execute_command(
                        "opkg update && opkg install uhttpd-mod-ubus"
                    )

            # Restart web server to ensure changes are loaded
            await ssh.execute_command("/etc/init.d/uhttpd restart 2>/dev/null || true")
            await ssh.disconnect()

            # Now, test the connection again using original config data!
            test_error = await self._test_connection(self._data)
            if test_error is None:
                # Success! Let's register unique ID and show success step
                await self._async_set_unique_id_and_check()
                return await self.async_step_repair_success()
            else:
                raise Exception(
                    f"Connection test still failed after repair. Error: {test_error}"
                )

        except Exception as err:
            _LOGGER.exception("Automatic repair failed: %s", err)
            return self.async_show_form(
                step_id="repair_failed",
                description_placeholders={"error": str(err)},
            )

    async def async_step_repair_success(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show success message after repair."""
        if user_input is not None:
            if self._data.get(CONF_USERNAME) == "root":
                return await self.async_step_provision_user()
            if self._permissions:
                if self._data.get(CONF_CONNECTION_TYPE) == CONNECTION_TYPE_UBUS:
                    return await self.async_step_permissions_ubus()
                return await self.async_step_permissions()
            if self._packages:
                return await self.async_step_packages()
            return self.async_create_entry(
                title=str(
                    self._device_info.get("hostname")
                    or self._data.get(CONF_HOST)
                    or "OpenWrt"
                ),
                data=self._data,
            )

        return self.async_show_form(
            step_id="repair_success",
            data_schema=vol.Schema({}),
        )

    async def async_step_display_new_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Display the new user credentials and ask to use them."""
        if user_input is not None:
            if user_input.get("use_new_user"):
                self._data[CONF_USERNAME] = "homeassistant"
                self._data[CONF_PASSWORD] = self._generated_password

                # Wait for services to fully restart after provisioning
                # Slower devices need more time for rpcd to come back up
                # We wait 10s now initially as it's a critical phase
                _LOGGER.info(
                    "Provisioning finished. Waiting 10s for router services to restart...",
                )
                await asyncio.sleep(10)

                # Re-check permissions with new user with retries
                new_user_success = False
                for attempt in range(10):
                    _LOGGER.info(
                        "Testing connection with new user 'homeassistant' (attempt %s/10)",
                        attempt + 1,
                    )
                    # Use a fresh connection test to avoid session leakage
                    error = await self._test_connection(self._data)
                    if not error:
                        _LOGGER.info(
                            "Connection with new user successful on attempt %s",
                            attempt + 1,
                        )
                        new_user_success = True
                        break

                    _LOGGER.warning(
                        "Auth attempt %s failed for %s: %s. Router might still be restarting services. Waiting 5s...",
                        attempt + 1,
                        self._data.get(CONF_HOST),
                        error,
                    )
                    await asyncio.sleep(5)

                if not new_user_success:
                    _LOGGER.error(
                        "Failed to connect with new user 'homeassistant' after 10 attempts at %s. "
                        "Config might have applied but services didn't pick it up or user creation failed. "
                        "Check your router logs for 'ha-openwrt' tags. Last error: %s",
                        self._data.get(CONF_HOST),
                        error,
                    )
                    return await self.async_step_provision_user(
                        errors={"base": error or "invalid_auth"},
                    )
            return await self.async_step_permissions()

        return self.async_show_form(
            step_id="display_new_user",
            data_schema=vol.Schema({vol.Required("use_new_user", default=True): bool}),
            description_placeholders={
                "username": "homeassistant",
                "password": self._generated_password or "",
            },
        )
