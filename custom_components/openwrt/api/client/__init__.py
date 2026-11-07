"""Client mixins for OpenWrt API."""

from __future__ import annotations

from .aggregator import OpenWrtAggregatorMixin
from .helpers import OpenWrtClientHelpersMixin
from .mesh import OpenWrtMeshMixin
from .status import OpenWrtStatusMixin

__all__ = [
    "OpenWrtAggregatorMixin",
    "OpenWrtClientHelpersMixin",
    "OpenWrtMeshMixin",
    "OpenWrtStatusMixin",
]
