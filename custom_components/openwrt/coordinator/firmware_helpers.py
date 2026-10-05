"""Firmware helpers and target mapping for OpenWrt coordinator."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..api.base import OpenWrtData

_LOGGER = logging.getLogger(__name__)

# Map of legacy/deprecated snapshot targets to their modern equivalents.
# OpenWrt periodically consolidates targets (e.g. the AX generation moved to qualcommax).
SNAPSHOT_TARGET_MAP = {
    "ipq807x/generic": "qualcommax/ipq807x",
    "ipq60xx/generic": "qualcommax/ipq60xx",
    "ipq50xx/generic": "qualcommax/ipq50xx",
    "ipq806x/generic": "qualcommax/ipq806x",
    "mediatek/mt7981": "mediatek/filogic",
    "mediatek/mt7986": "mediatek/filogic",
    "mediatek/mt7622": "mediatek/filogic",
    "mediatek/mt7623": "mediatek/filogic",
    "rockchip/armv8": "rockchip/rk3328",
    "ipq807x": "qualcommax/ipq807x",
    "ipq60xx": "qualcommax/ipq60xx",
    "ipq50xx": "qualcommax/ipq50xx",
    "qualcommax/generic": "qualcommax/ipq807x",
}


def parse_repo(repo_input: str) -> tuple[str, str]:
    """Parse 'owner/repo' from URL or direct input."""
    repo_input = repo_input.strip().strip("/")
    url_match = re.search(r"github\.com/([^/]+)/([^/]+)", repo_input)
    if url_match:
        return url_match.group(1), url_match.group(2)
    parts = repo_input.split("/")
    return (parts[0], parts[1]) if len(parts) == 2 else ("", repo_input)


def build_sysupgrade_pattern(data: OpenWrtData) -> str | None:
    """Build regex pattern for sysupgrade matching.

    OpenWrt firmware filenames use the format:
      openwrt-{subtarget}-{arch}-{board}-sysupgrade.bin
    but the router reports target as "{arch}/{subtarget}" (e.g.
    "qualcommax/ipq807x"). The two parts can appear in either order in the
    filename, so we use lookaheads to require both parts independently.
    """
    info = data.device_info
    if not info.target or not info.board_name:
        return None
    # Split "arch/subtarget" into its two components so we can match them
    # regardless of their order in the filename.
    target_parts = info.target.split("/")
    board = info.board_name.replace(",", "_").replace(" ", "_")
    # Build a lookahead for every target part + the board name
    lookaheads = "".join(rf"(?=.*{re.escape(part)})" for part in target_parts if part)
    lookaheads += rf"(?=.*{re.escape(board)})"
    lookaheads += r"(?=.*sysupgrade\.bin)"
    return rf"^{lookaheads}.*$"


def version_is_newer(current: str, latest: str) -> bool:
    """Compare firmware versions (e.g., '24.10.1' vs '25.12.0')."""
    if current and latest and current == latest:
        return False

    if "SNAPSHOT" in current.upper() or "SNAPSHOT" in latest.upper():
        # For snapshots, we always prefer revision comparison if possible
        def get_rev_num(v: str) -> int:
            # Matches r12345 or SNAPSHOT (r12345)
            match = re.search(r"r(\d+)", v)
            if match:
                return int(match.group(1))
            return -1

        rev_current = get_rev_num(current)
        rev_latest = get_rev_num(latest)

        _LOGGER.debug(
            "Comparing snapshots: current=%s (rev=%s), latest=%s (rev=%s)",
            current,
            rev_current,
            latest,
            rev_latest,
        )

        if rev_current >= 0 and rev_latest >= 0:
            if rev_latest != rev_current:
                return rev_latest > rev_current

        # Check for embedded date strings (e.g. 2026-07-13-2054 or 2026-07-13) in latest/current
        date_match_current = re.search(
            r"(\d{4}[-._]\d{2}[-._]\d{2}(?:[-._]\d{4})?)", current
        )
        date_match_latest = re.search(
            r"(\d{4}[-._]\d{2}[-._]\d{2}(?:[-._]\d{4})?)", latest
        )
        if date_match_latest and not date_match_current:
            # Latest has a date tag (e.g. 2026-07-13) and current is generic SNAPSHOT -> latest is newer
            return True
        if date_match_current and date_match_latest:
            c_str = date_match_current.group(1).replace(".", "-").replace("_", "-")
            l_str = date_match_latest.group(1).replace(".", "-").replace("_", "-")
            if c_str != l_str:
                return l_str > c_str

        # Fallback to string comparison if revisions aren't numeric/comparable
        # but strip "SNAPSHOT" and extra chars for a cleaner comparison
        clean_current = re.sub(
            r"[^a-zA-Z0-9-]", "", current.upper().replace("SNAPSHOT", "")
        )
        clean_latest = re.sub(
            r"[^a-zA-Z0-9-]", "", latest.upper().replace("SNAPSHOT", "")
        )
        result = clean_latest != clean_current
        _LOGGER.debug(
            "Snapshot fallback comparison: %s != %s -> %s",
            clean_latest,
            clean_current,
            result,
        )
        return result

    try:
        current_parts = [int(p) for p in current.split(".")]
        latest_parts = [int(p) for p in latest.split(".")]
        return latest_parts > current_parts
    except (
        ValueError,
        AttributeError,
    ):
        return current != latest
