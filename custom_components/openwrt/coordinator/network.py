"""Network rates and MWAN tracking mixin for OpenWrt coordinator."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from ..api.base import OpenWrtData

if TYPE_CHECKING:
    from .base import CoordinatorBase

    _Base = CoordinatorBase
else:
    _Base = object

_LOGGER = logging.getLogger(__name__)

_MWAN_SESSION_TOLERANCE = 5.0


class NetworkMixin(_Base):
    """Mixin for network interface rate calculations and MWAN online ratios."""

    async def _async_update_mwan_boot_totals(
        self, data: OpenWrtData, system_uptime: int, rebooted: bool
    ) -> None:
        """Maintain the MWAN3 online ratio since the router last booted.

        mwan3track does not store durations. On every state change it writes
        the current system uptime into one of two files and zeroes the other
        (connected() sets ONLINE, disconnected() sets OFFLINE), and ubus turns
        whichever is set into a seconds count at query time. So exactly one of
        "online" and "offline" is ever non-zero, and subtracting it from the
        system uptime of the same snapshot recovers the uptime second at which
        that state began - two router-side values, no clock involved:

            window = system uptime - first online second
            outage = completed offline spans + the one currently open
            ratio  = (window - outage) / window

        Working on the router's monotonic uptime rather than on a wall clock
        keeps the two sides of the fraction exact. It also makes the metric
        survive a Home Assistant restart intact: the counters keep running on
        the router, so an outage that happens while nothing is watching is
        still fully reflected in the next sample.

        The window opens at the first moment mwan3 declared the interface
        online, not at boot. Everything before that - drivers, DHCP, a modem
        dialling in, and mwan3 needing several successful checks before it
        commits - is the router coming up rather than an outage of the line.
        Until that first moment there is no window and no ratio is published.

        Where a transition is missed between two polls its start is unknown;
        it is then taken to be as early as it possibly can, so the ratio errs
        low and never overstates availability. The window actually covered is
        published as the "coverage_start" attribute.
        """
        if not data.mwan_status or system_uptime <= 0:
            return

        now = dt_util.utcnow()
        boot = self._boot_time
        changed = False

        if not self._mwan_totals_loaded:
            self._mwan_totals_loaded = True
            try:
                stored = await self._mwan_store.async_load()
            except Exception:  # noqa: BLE001
                # Best effort, like the save path below: without the
                # stored totals the ratio re-anchors at the next sample
                # instead of carrying on.
                _LOGGER.debug("Could not load MWAN3 totals", exc_info=True)
                stored = None
            if stored and boot is not None:
                stored_boot = stored.get("boot_epoch")
                if (
                    isinstance(stored_boot, (int, float))
                    and abs(stored_boot - boot.timestamp()) <= 120
                ):

                    def _scalars(raw: Any) -> dict[str, float]:
                        out: dict[str, float] = {}
                        if isinstance(raw, dict):
                            for k, v in raw.items():
                                if isinstance(v, (int, float)):
                                    out[str(k)] = float(v)
                        return out

                    self._mwan_first_online = _scalars(stored.get("first_online"))
                    self._mwan_offline_total = _scalars(stored.get("offline_total"))
                    self._mwan_offline_since = _scalars(stored.get("offline_since"))
                    self._mwan_last_uptime = _scalars(stored.get("last_uptime"))
                    self._mwan_online_start = _scalars(stored.get("online_start"))
                    self._mwan_boot_epoch = float(stored_boot)

        # Tie the reset to the boot time itself rather than to the
        # coordinator's "rebooted" flag: that flag is derived from an
        # in-memory uptime comparison and is lost when the coordinator is
        # rebuilt or when stale values are carried through the router's
        # downtime. The boot time is what this metric is anchored to, so
        # comparing it is both the correct test and self-healing.
        boot_ts = boot.timestamp() if boot is not None else None
        boot_changed = (
            boot_ts is not None
            and self._mwan_boot_epoch is not None
            and abs(boot_ts - self._mwan_boot_epoch) > 120
        )
        reset = rebooted or boot_changed
        # The last sample of each interface before the reboot: a failed query
        # right after it hands back exactly that reply.
        before_reboot = self._mwan_sample if reset else {}
        if reset:
            self._mwan_first_online = {}
            self._mwan_offline_total = {}
            self._mwan_offline_since = {}
            self._mwan_last_uptime = {}
            self._mwan_online_start = {}
            self._mwan_sample = {}
            changed = True
        if boot_ts is not None and self._mwan_boot_epoch != boot_ts:
            self._mwan_boot_epoch = boot_ts
            changed = True

        # Drop samples that belong to the previous boot. A tracking session
        # cannot be older than the router itself, and a reply identical to the
        # last one before the reboot was handed back by a failed query - a
        # real reply always differs, as its counters advance every second.
        # The state and ratio of such a sample describe the previous boot,
        # and mixing it with the fresh uptime would place the transitions
        # derived from it at the wrong second.
        data.mwan_status = [
            m
            for m in data.mwan_status
            if m.uptime <= system_uptime + 60
            and before_reboot.get(m.interface_name) != (m.uptime, m.online, m.offline)
        ]

        for m in data.mwan_status:
            name = m.interface_name
            # Publish only what this update computes: a reply handed back by a
            # failed query still carries the values of an earlier update.
            m.boot_online_ratio = None
            m.coverage_start = None

            first = self._mwan_first_online.get(name)
            if first is not None and first > system_uptime:
                # The uptime went backwards without a reboot being detected;
                # the stored second no longer refers to this boot.
                self._mwan_first_online.pop(name, None)
                self._mwan_offline_total.pop(name, None)
                self._mwan_offline_since.pop(name, None)
                self._mwan_online_start.pop(name, None)
                first = None
                changed = True

            # Only draw conclusions from a snapshot that actually is one. On
            # a failed call the client hands back the previous reply, and a
            # real reply always differs: while an interface is up "online"
            # advances every second, while it is down "offline" does. So an
            # identical triple means no new information - and pairing it with
            # a fresh system uptime would shift the reconstructed transition
            # instants forward and invent outages that never happened. Right
            # after a reboot, when ubus is slow to answer, that is common.
            sample = (m.uptime, m.online, m.offline)
            fresh = self._mwan_sample.get(name) != sample
            self._mwan_sample[name] = sample

            since = self._mwan_offline_since.get(name)
            last_up = self._mwan_last_uptime.get(name)
            prev_start = self._mwan_online_start.get(name)

            if fresh and m.online > 0:
                online_start = float(system_uptime) - m.online
                if first is None:
                    # The first online moment we can see opens the window.
                    self._mwan_first_online[name] = online_start
                    self._mwan_offline_total.setdefault(name, 0.0)
                    self._mwan_online_start[name] = online_start
                    changed = True
                elif since is not None:
                    # The open outage ended exactly when the interface went
                    # online, which both counters date precisely - including
                    # the part that fell between two polls.
                    self._mwan_offline_total[name] = self._mwan_offline_total.get(
                        name, 0.0
                    ) + max(online_start - since, 0.0)
                    self._mwan_offline_since.pop(name, None)
                    self._mwan_online_start[name] = online_start
                    changed = True
                elif prev_start is None or online_start < prev_start:
                    # Keep the earliest start seen for this spell: mwan3 does
                    # not rewrite the stamp while the interface stays up, so
                    # the lowest reading is the one least affected by skew.
                    self._mwan_online_start[name] = online_start
                    changed = True
                elif online_start > prev_start + _MWAN_SESSION_TOLERANCE:
                    # The stamp really did move: a new spell began, so the
                    # interface was down in between. Its end is dated, its
                    # start is not - count it from the last poll on, the
                    # longest it can have been, so the ratio errs low.
                    base = prev_start if last_up is None else max(last_up, prev_start)
                    gap = max(online_start - base, 0.0)
                    self._mwan_offline_total[name] = (
                        self._mwan_offline_total.get(name, 0.0) + gap
                    )
                    self._mwan_online_start[name] = online_start
                    _LOGGER.debug(
                        "MWAN3 %s: unobserved outage of %.0fs (online spell moved "
                        "from uptime %.0f to %.0f, last poll at %s)",
                        name,
                        gap,
                        prev_start,
                        online_start,
                        last_up,
                    )
                    changed = True
            elif fresh and m.offline > 0 and first is not None:
                offline_start = float(system_uptime) - m.offline
                new_since = (
                    offline_start if since is None else min(since, offline_start)
                )
                if new_since != since:
                    # Keep the earliest start of the open outage. A later one
                    # means an online spell in between went unseen; counting
                    # the whole span as offline is the conservative reading.
                    self._mwan_offline_since[name] = new_since
                    changed = True

            if fresh:
                self._mwan_last_uptime[name] = float(system_uptime)

            first = self._mwan_first_online.get(name)
            if first is None:
                continue

            # Whole seconds on both sides: the router's counters are integers
            # and mixing them with fractional ones makes the quotient wobble
            # in its fifth decimal every cycle, writing a recorder row for a
            # value that has not meaningfully changed.
            window = float(int(system_uptime) - int(first))
            # Below a minute the quotient is dominated by rounding, so report
            # nothing rather than a misleading 0 or 100.
            if window >= 60:
                outage = self._mwan_offline_total.get(name, 0.0)
                open_since = self._mwan_offline_since.get(name)
                if open_since is not None:
                    outage += max(float(system_uptime) - open_since, 0.0)
                m.boot_online_ratio = min(max(window - outage, 0.0) / window, 1.0)
            m.coverage_start = now - timedelta(seconds=float(system_uptime) - first)

        if changed and boot is not None:
            try:
                await self._mwan_store.async_save(
                    {
                        "boot_epoch": boot.timestamp(),
                        "first_online": self._mwan_first_online,
                        "offline_total": self._mwan_offline_total,
                        "offline_since": self._mwan_offline_since,
                        "last_uptime": self._mwan_last_uptime,
                        "online_start": self._mwan_online_start,
                    }
                )
            except Exception:  # noqa: BLE001
                # Persisting is best effort, mirroring the load path above.
                # Without it the ratio re-anchors after a restart instead of
                # carrying on - a worse number, but no reason to bring the
                # whole update cycle down with it.
                _LOGGER.debug("Could not persist MWAN3 totals", exc_info=True)

    def _async_process_network_rates(self, data: OpenWrtData, now: float) -> None:
        """Calculate network rates."""
        elapsed = now - self._last_update_time
        if self._last_update_time > 0 and elapsed > 0:
            for iface in data.network_interfaces:
                prev = self._prev_network_stats.get(iface.name)
                if prev:
                    rx_diff = iface.rx_bytes - prev.get("rx_bytes", 0)
                    tx_diff = iface.tx_bytes - prev.get("tx_bytes", 0)
                    if rx_diff >= 0 and tx_diff >= 0:
                        iface.rx_rate = round(
                            (rx_diff * 8) / (1024 * 1024) / elapsed, 2
                        )
                        iface.tx_rate = round(
                            (tx_diff * 8) / (1024 * 1024) / elapsed, 2
                        )

        for iface in data.network_interfaces:
            self._prev_network_stats[iface.name] = {
                "rx_bytes": iface.rx_bytes,
                "tx_bytes": iface.tx_bytes,
            }
