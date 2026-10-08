"""Structural discovery of a CE's PE-side access LAGs.

A CE (an access switch, say) is *logical*: the CE ``Device`` row itself owns no
interfaces. Everything built when it was attached lives on the PE side
— a UNI LAG per PE, one or two member ports, and (dual-homed only) an EVPN ESI.
See ``CEAttachmentBuilder`` for the build side.

There is no foreign key from the LAG back to the CE device; the only link is the
interface *description* the attachment builder writes (``CE:{ce_name} (access LAG
id N)`` on the LAG). This module therefore discovers a CE's LAGs *structurally* by
that description — which also means "what is a CE" needs no hardcoded role list:
any device that has an access LAG described ``CE:{host}`` is a CE, whether it is
a ``switch``, a ``test-switch`` or a future role.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Interface

_LAG_DESC_PREFIX = "CE:"


def _ce_name_from_lag(description: str) -> str:
    """Extract the CE hostname from a LAG description.

    ``CE:{ce_name} (access LAG id N)`` → ``{ce_name}``. The `` (`` boundary is
    unambiguous because hostnames contain no space.
    """
    return description.removeprefix(_LAG_DESC_PREFIX).split(" (", 1)[0]


def find_ce_lags(session: Session) -> dict[str, list[Interface]]:
    """Return a mapping of CE hostname → its PE-side access LAG interface(s).

    Discovered structurally: LAG parents (``parent_id IS NULL``) whose
    description starts ``CE:``. A dual-homed CE yields two LAGs (one per PE of
    the pair); a single-homed CE yields one.
    """
    lags = session.scalars(
        select(Interface).where(
            Interface.description.like(f"{_LAG_DESC_PREFIX}%"),
            Interface.parent_id.is_(None),
        )
    ).all()

    by_ce: dict[str, list[Interface]] = {}
    for lag in lags:
        if not lag.description:
            continue
        ce_name = _ce_name_from_lag(lag.description)
        by_ce.setdefault(ce_name, []).append(lag)
    return by_ce
