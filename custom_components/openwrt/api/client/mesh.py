# mypy: disable-error-code="attr-defined,has-type"
"""Batman-adv mesh client mixin for OpenWrt."""

from __future__ import annotations

import contextlib
import logging
import re
from typing import Any

from ..models import BatmanGateway, BatmanNeighbor, BatmanOriginator

_LOGGER = logging.getLogger(__name__)


class OpenWrtMeshMixin:
    """Mixin for Batman-adv mesh operations."""

    async def get_batman_data(self) -> dict[str, Any]:
        """Get Batman-adv mesh data via batctl CLI.

        This is a shared implementation for all clients that have execute_command.
        """
        data: dict[str, Any] = {
            "originators": [],
            "neighbors": [],
            "gateways": [],
            "translation_table": {},
        }

        # Originators
        try:
            out = await self.execute_command("batctl o -H 2>/dev/null")
            if out:
                for line in out.strip().splitlines():
                    is_best = "*" in line
                    line_clean = line.replace("*", "").strip()
                    parts = line_clean.split()
                    if len(parts) >= 5:
                        # Try to find TQ - either in parens or at the end
                        tq = 0
                        for p in parts:
                            if p.startswith("(") and p.endswith(")"):
                                with contextlib.suppress(ValueError):
                                    tq = int(p.strip("()"))
                                    break
                        if tq == 0 and parts[-1].isdigit():
                            tq = int(parts[-1])

                        data["originators"].append(
                            BatmanOriginator(
                                mac=parts[0].upper(),
                                last_seen=float(parts[1].strip("s")),
                                tq=tq,
                                next_hop=parts[3].upper() if len(parts) > 3 else "",
                                outgoing_iface=parts[4].strip("[]:"),
                                is_best=is_best,
                            )
                        )
        except Exception as err:
            _LOGGER.debug("Failed to get Batman originators: %s", err)

        # Neighbors
        try:
            out = await self.execute_command("batctl n -H 2>/dev/null")
            if out:
                for line in out.strip().splitlines():
                    parts = line.split()
                    if len(parts) >= 3:
                        data["neighbors"].append(
                            BatmanNeighbor(
                                mac=parts[1].upper(),
                                last_seen=float(parts[2].strip("s")),
                                interface=parts[0].strip("[]"),
                            )
                        )
        except Exception as err:
            _LOGGER.debug("Failed to get Batman neighbors: %s", err)

        # Gateways
        try:
            out = await self.execute_command("batctl gwl -H 2>/dev/null")
            if out:
                for line in out.strip().splitlines():
                    is_selected = "=>" in line or "*" in line
                    line_clean = line.replace("=>", "").replace("*", "").strip()
                    parts = line_clean.split()
                    if len(parts) >= 5:
                        bw_parts = parts[4].strip(":").split("/")
                        data["gateways"].append(
                            BatmanGateway(
                                mac=parts[0].upper(),
                                tq=(
                                    int(parts[1].strip("()"))
                                    if "(" in parts[1]
                                    else (int(parts[1]) if parts[1].isdigit() else 0)
                                ),
                                next_hop=parts[2].upper(),
                                outgoing_iface=parts[3].strip("[]"),
                                is_selected=is_selected,
                                bandwidth_down=bw_parts[0] if len(bw_parts) > 0 else "",
                                bandwidth_up=bw_parts[1] if len(bw_parts) > 1 else "",
                            )
                        )
        except Exception as err:
            _LOGGER.debug("Failed to get Batman gateways: %s", err)

        # Translation Table
        try:
            out = await self.execute_command("batctl tg -H 2>/dev/null")
            if out:
                for line in out.strip().splitlines():
                    line_clean = line.replace("*", "").strip()
                    parts = line_clean.split()
                    if len(parts) >= 4:
                        # Format usually: [MAC] [VID] [Flags] [Last seen] [Originator]
                        # But can vary. We look for two MACs.
                        macs = [
                            p.upper()
                            for p in parts
                            if re.match(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", p, re.I)
                        ]
                        if len(macs) >= 2:
                            data["translation_table"][macs[0]] = macs[1]
        except Exception as err:
            _LOGGER.debug("Failed to get Batman translation table: %s", err)

        # Determine if mesh is active (has any data)
        has_tables = any([data["originators"], data["neighbors"], data["gateways"]])
        data["tables_supported"] = True
        if not has_tables:
            data["tables_supported"] = await self._batctl_tables_supported()
        if data["tables_supported"] is True:
            data["mesh_active"] = has_tables
        else:
            # batctl-tiny (False) cannot list originators/neighbors/gateways and
            # unreadable tables (None) say nothing either, so the mesh would
            # always look inactive. Fall back to the hard interface state,
            # which batctl-tiny still reports.
            data["mesh_active"] = await self._batman_hardif_active()
        return data

    async def _batctl_tables_supported(self) -> bool | None:
        """Probe whether batctl can list its debug tables.

        Returns True when it can (an empty table still prints its
        "[B.A.T.M.A.N. adv ...]" header), False for batctl-tiny, which only
        ships the configuration subcommands ("o", "n", "gwl" and "tg" print the
        usage text to stderr and nothing to stdout), and None when the probe
        failed, returned nothing or returned an error (e.g. "batctl: not found"
        or "interface bat0 is not present"), so the tables could not be read.
        """
        try:
            out = await self.execute_command("batctl o 2>&1")
        except Exception as err:
            _LOGGER.debug("Failed to probe batctl capabilities: %s", err)
            return None
        if not out or not out.strip():
            _LOGGER.debug("batctl capability probe returned no output")
            return None
        if "[B.A.T.M.A.N. adv" in out:
            return True
        if "Usage:" not in out:
            _LOGGER.debug(
                "batctl capability probe failed: %s", out.strip().splitlines()[0]
            )
            return None
        if not getattr(self, "_batctl_tiny_warned", False):
            self._batctl_tiny_warned = True
            _LOGGER.warning(
                "%s: batctl cannot list Batman originators/neighbors/gateways "
                "(batctl-tiny?). Install batctl-default or batctl-full for "
                "Batman mesh statistics",
                self.host,
            )
        return False

    async def _batman_hardif_active(self) -> bool:
        """Return True if batman-adv has at least one active hard interface."""
        try:
            out = await self.execute_command("batctl if 2>/dev/null")
        except Exception as err:
            _LOGGER.debug("Failed to get Batman interfaces: %s", err)
            return False
        return any(
            line.strip().endswith(": active") for line in (out or "").splitlines()
        )
