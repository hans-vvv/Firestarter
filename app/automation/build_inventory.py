"""CLI: rebuild the Nornir inventory from the topology database.

Usage::

    python app/automation/build_inventory.py

Writes ``hosts.yaml`` and ``groups.yaml`` into ``automation/generated/`` under the
data root, overwriting whatever was there — the inventory is derived state, regenerated
fresh for every run. Prints what it targeted so the operator can see the scope
*before* pointing a task at real equipment.

The logic lives in :mod:`app.automation.inventory` so the dashboard can call it
directly; this module is only the command-line wrapper.
"""

from __future__ import annotations

import sys
from pathlib import Path

# app/automation/build_inventory.py → parents[2] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _ensure_repo_root() -> None:
    """Put the repo root on ``sys.path`` so ``app.*`` imports resolve.

    Running this file directly puts ``app/automation/`` on the path, not the repo
    root, so ``import app`` fails. Mirrors ``compliance_snapshot._ensure_repo_root``
    and is likewise called lazily from :func:`main`, so importing this module in a
    test has no side effects. No ``chdir`` is needed here — every path this tool
    touches is absolute.
    """
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))


def main() -> int:
    _ensure_repo_root()
    from app.automation.inventory import InventoryError, build_inventory
    from app.utils import db_session

    try:
        with db_session() as session:
            result = build_inventory(session=session)
    except InventoryError as exc:
        # A resolution failure is an operator problem (a device missing its
        # management address), not a crash — report it plainly.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {result.hosts_file}")
    print(f"Wrote {result.groups_file}")
    print(f"\n{len(result.hosts)} active device(s), {len(result.roles)} role(s):")
    for host in result.hosts:
        print(f"  {host.hostname:<24} {host.address:<16} {host.role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
