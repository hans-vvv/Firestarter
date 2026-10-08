"""Shared convention for the CE-management VPRN's delegated prefixes.

A VPRN whose ``subnet_info`` carries ``ce_mgmt: true`` is the one that gives
every CE behind a PE (or PE pair) a management address: the VPRN feature
handler allocates one delegated prefix per ``pair_label`` for it, and the CE
management allocator (``ce_mgmt_allocator``) later reads those prefixes back to
hand each CE a sticky host address inside them.

The two sides only meet through the ``DelegatedPrefix.name`` they agree on, so
the naming lives here and both import it: if they drifted the allocator would
find no prefixes and every CE would fail allocation. The name is independent of
the VPRN's service/variant name on purpose — renaming the YAML variant must not
orphan the CE addresses already handed out.
"""

from __future__ import annotations

CE_MGMT_ALLOC_PREFIX = "ce_mgmt_"


def ce_mgmt_allocation_name(*, pair_label: str) -> str:
    """``DelegatedPrefix.name`` of the CE-management subnet of one PE / PE pair."""
    return f"{CE_MGMT_ALLOC_PREFIX}{pair_label}"
