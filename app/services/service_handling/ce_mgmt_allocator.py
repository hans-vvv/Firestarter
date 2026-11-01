"""Sticky management-IP allocation for CEs (access switches and the like).

Every CE sits behind a PE (or PE pair) whose CE-management VPRN — the VPRN whose
``subnet_info`` carries ``ce_mgmt: true`` — owns a delegated ``/28`` (one per
``pair_label``, stored in ``DelegatedPrefix`` under the name
``ce_mgmt_{pair_label}``; see ``app.domain.ce_mgmt``). This module assigns each
CE a stable host address out of that ``/28`` and persists it in
``CeMgmtAddress``.

Allocation rule (one rule covers first-run and incremental):

* Reserve the first three usable hosts (``.1`` VRRP gateway, ``.2``/``.3`` the
  two PE IRBs — see ``feature_handlers/vprn._resolve_irb_ip_address``);
  CEs start at the **4th usable host**.
* Assign every *unassigned* CE, in Excel ``CEs``-sheet row order, to the lowest
  free host ``>= .4`` in its ``/28``.

Because assignments are persisted, the first run fills each ``/28`` in Excel
order while every later run only places genuinely new CEs — existing addresses
never move (**sticky**). Orphaned rows (a CE that no longer has an access LAG,
e.g. after decommission) are pruned first so their slot is freed for reuse.

A contiguity guardrail rejects gaps in the per-site CE index sequence (e.g.
``switch3.<site>`` present while ``switch2.<site>`` is missing), so the sticky
low-fill stays predictable.

The table is deliberately *not* consumed by any render template — it backs the
``report_ce_mgmt`` report and reserves addresses for possible future config
use. See ``app.models.CeMgmtAddress``.
"""

from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.ce_mgmt import CE_MGMT_ALLOC_PREFIX
from app.domain.file_locations import TOPOLOGY_EXCEL_LOC
from app.models import CeMgmtAddress, DelegatedPrefix, Interface
from app.services.service_handling.ce_lags import find_ce_lags
from app.utils import load_sheet

# Hosts .1/.2/.3 in each /28 are reserved by the CE-management VPRN itself: .1
# is the VRRP virtual gateway (network+1) and .2/.3 are the two PE IRB addresses
# (network+2 / network+3). CEs therefore take the 4th usable host onward,
# uniformly — even a standalone PE that only uses .1/.2 keeps .3 reserved
# (the "worst case three reserved" rule keeps numbering identical whether a
# site has one or two PE devices).
_FIRST_CE_HOST_OFFSET = 4

# A CE hostname is ``{node}.{site}``; the node is a stem plus a trailing index
# (``switch3`` -> stem ``switch``, index 3; ``test-switch2`` -> ``test-switch``, 2).
_CE_NODE_INDEX_RE = re.compile(r"^(?P<stem>.*?)(?P<index>\d+)$")


def allocate_ce_mgmt_addresses(
    *,
    session: Session,
    wb_name: str | Path = TOPOLOGY_EXCEL_LOC.path,
) -> list[CeMgmtAddress]:
    """Assign & persist a sticky management IP to every CE. Idempotent.

    Returns all current ``CeMgmtAddress`` rows, ordered by address (which, since
    each /28 is disjoint, is network-then-host order).
    """
    excel_order = _load_excel_ce_order(wb_name)
    validate_ce_index_contiguity(excel_order)

    lags_by_ce = find_ce_lags(session)  # authoritative set of *attached* CEs
    prefix_by_pair = _delegated_prefix_by_pair(session)

    # Iterate in Excel row order; any attached CE absent from the sheet comes
    # last, ordered by hostname, so allocation stays deterministic.
    order_index = {name: i for i, name in enumerate(excel_order)}
    ordered_ces = sorted(
        lags_by_ce,
        key=lambda h: (order_index.get(h, len(order_index)), h),
    )

    _prune_orphans(session, valid_ce_hostnames=set(lags_by_ce))

    assigned: dict[str, CeMgmtAddress] = {
        row.ce_hostname: row for row in session.scalars(select(CeMgmtAddress)).all()
    }
    used_by_prefix: dict[str, set[str]] = defaultdict(set)
    for row in assigned.values():
        used_by_prefix[row.prefix].add(row.address)

    for ce in ordered_ces:
        if ce in assigned:
            continue  # sticky — never re-place an existing assignment

        pair_label = _pair_label_for_ce(ce, lags_by_ce[ce])
        prefix = prefix_by_pair.get(pair_label)
        if prefix is None:
            raise ValueError(
                f"No CE-management delegated /28 found for CE {ce!r} "
                f"(pair_label={pair_label!r}); expected a DelegatedPrefix named "
                f"{CE_MGMT_ALLOC_PREFIX}{pair_label} — does a VPRN carry "
                f"subnet_info.ce_mgmt: true?"
            )

        address = _next_free_host(prefix, used_by_prefix[prefix])
        row = CeMgmtAddress(
            ce_hostname=ce,
            pair_label=pair_label,
            prefix=prefix,
            address=address,
        )
        session.add(row)
        used_by_prefix[prefix].add(address)
        assigned[ce] = row

    session.flush()
    return sorted(assigned.values(), key=lambda r: ipaddress.ip_address(r.address))


def validate_ce_index_contiguity(ce_hostnames: list[str]) -> None:
    """Raise if any per-site CE index sequence has a gap.

    CEs are grouped by ``(site, stem)`` (e.g. all ``switch*.<site>`` together);
    each group's indices must run ``1..N`` with no holes. A gap means a
    lower-indexed CE is missing while a higher-indexed one exists — e.g.
    ``switch3.<site>`` without ``switch2.<site>`` — which would make sticky
    low-fill numbering ambiguous.

    CEs whose node has no trailing index carry no contiguity constraint and are
    ignored.
    """
    groups: dict[tuple[str, str], dict[int, str]] = defaultdict(dict)
    for host in ce_hostnames:
        node, _, site = host.partition(".")
        match = _CE_NODE_INDEX_RE.match(node)
        if not match:
            continue
        stem = match.group("stem")
        index = int(match.group("index"))
        groups[(site, stem)][index] = host

    missing: list[str] = []
    for (site, stem), index_map in sorted(groups.items()):
        for i in range(1, max(index_map) + 1):
            if i not in index_map:
                missing.append(f"{stem}{i}.{site}")

    if missing:
        raise ValueError(
            "CE index gap(s) detected — a higher-indexed CE exists while these "
            "lower-indexed peers are absent (add them, or renumber): " + ", ".join(missing)
        )


def _load_excel_ce_order(wb_name: str | Path) -> list[str]:
    """Return the ``CEname`` values from the ``CEs`` sheet in row order.

    ``load_sheet`` already drops blank separator rows and normalises empty cells
    to ``None``, so only real CE names survive.
    """
    df = load_sheet(sheet_name="CEs", wb_name=wb_name)
    order: list[str] = []
    for record in df.to_dict("records"):
        name = record.get("CEname")
        if name:
            order.append(str(name).strip())
    return order


def _delegated_prefix_by_pair(session: Session) -> dict[str, str]:
    """Map ``pair_label`` -> its CE-management ``/28`` from the DelegatedPrefix rows."""
    rows = session.scalars(
        select(DelegatedPrefix).where(DelegatedPrefix.name.like(f"{CE_MGMT_ALLOC_PREFIX}%"))
    ).all()

    by_pair: dict[str, str] = {}
    for row in rows:
        pair_label = row.name[len(CE_MGMT_ALLOC_PREFIX) :]
        prefix = row.reservations.get("prefix")
        if prefix:
            by_pair[pair_label] = prefix
    return by_pair


def _pair_label_for_ce(ce_hostname: str, lags: list[Interface]) -> str:
    """Resolve the single ``pair_label`` a CE's access LAG(s) attach to.

    A dual-homed CE has two access LAGs, one on each PE of the pair; both
    PEs share the pair's ``pair_label``, so exactly one distinct label must
    result.
    """
    pair_labels = {(lag.device.labels.get("pair_label") or lag.device.hostname) for lag in lags}
    if len(pair_labels) != 1:
        raise ValueError(
            f"CE {ce_hostname!r} maps to {len(pair_labels)} pair_labels "
            f"({sorted(pair_labels)}); expected exactly one."
        )
    return next(iter(pair_labels))


def _next_free_host(prefix: str, used: set[str]) -> str:
    """Lowest free host ``>= _FIRST_CE_HOST_OFFSET``-th usable in ``prefix``.

    ``network.hosts()`` excludes the network and broadcast addresses, so the
    slice from index ``_FIRST_CE_HOST_OFFSET - 1`` starts at the 4th usable host.
    """
    network = ipaddress.ip_network(prefix, strict=True)
    for host in list(network.hosts())[_FIRST_CE_HOST_OFFSET - 1 :]:
        candidate = str(host)
        if candidate not in used:
            return candidate
    raise ValueError(
        f"CE-management subnet {prefix} is exhausted — no free host at or beyond the "
        f"{_FIRST_CE_HOST_OFFSET}th usable address."
    )


def _prune_orphans(session: Session, *, valid_ce_hostnames: set[str]) -> None:
    """Delete assignments whose CE is no longer attached, freeing their slots."""
    for row in session.scalars(select(CeMgmtAddress)).all():
        if row.ce_hostname not in valid_ce_hostnames:
            session.delete(row)
    session.flush()
