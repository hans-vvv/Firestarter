"""Bring a clean clone with a populated data root to a fully working demo.

Runs every step the dashboard would otherwise need an operator to click through,
in pipeline order, and prints one line per step. Idempotent: every step is safe
to re-run (the pipeline skips topology that exists, the backups and renders are
archived rather than overwritten, the admin seed is a no-op once a user exists),
so running it twice leaves the same working demo.

Prerequisites: the data root (``FIRESTARTER_DATA``, default ``<repo>/data``) holds
``topology.xlsx``, ``services/{definitions,addressing}/*.yaml`` and the compliance /
simulation inputs. The database is created here if missing.

Usage::

    FIRESTARTER_DEMO=1 .venv/bin/python scripts/bootstrap_demo.py

Steps, and what each one leaves behind under the data root:

1. ``alembic upgrade head``   — creates/migrates ``app.db`` (``alembic_version`` stamped)
2. ``ensure_schema``          — additive tables that have no migration yet
3. seed admin                 — roles + the bootstrap ``admin`` user
4. ``run_pipeline``           — Excel → topology → services (``app.db``)
5. activate devices           — every simulated router ``active``
6. ``render_all_to_disk``     — ``artifacts/latest/*.cfg``
7. ``run_simulated_backup``   — ``backups/latest/`` from the simulated devices
8. ``run_compliance``         — a first comparison (in-process summary + reports)
9. ``build_inventory``        — ``automation/generated/{hosts,groups}.yaml``
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

# scripts/bootstrap_demo.py → parents[1] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _step(name: str, fn: Callable[[], str]) -> None:
    """Run one step and print ``name ... detail (seconds)`` on a single line."""
    started = time.monotonic()
    detail = fn()
    print(f"{name:<28} {detail} ({time.monotonic() - started:.1f}s)")


def _alembic_upgrade() -> str:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    from app.config import get_database_url

    return f"schema at head — {get_database_url()}"


def _ensure_schema() -> str:
    from app.web import ensure_schema

    ensure_schema()
    return "additive tables present"


def _seed_admin() -> str:
    from app.utils import db_session
    from app.web import accounts

    with db_session() as session:
        accounts.seed_user_roles(session)
        accounts.seed_default_admin(session)
    mode = (
        "demo mode, no forced password change"
        if accounts.is_demo_mode()
        else "forced change on first login"
    )
    return f"user '{accounts.DEFAULT_ADMIN_USERNAME}' ({mode})"


def _run_pipeline() -> str:
    from app.pipeline import run_pipeline
    from app.utils import db_session

    with db_session() as session:
        result = run_pipeline(session=session)
    return f"job {result.job_name}"


def _activate_devices() -> str:
    from app.automation.simulated_devices import activate_simulated_devices
    from app.utils import db_session

    with db_session() as session:
        changed = activate_simulated_devices(session=session)
    return f"{changed} device(s) set active"


def _render() -> str:
    from app.web.utils import render_all_to_disk

    rendered = render_all_to_disk()
    return f"{len(rendered)} config(s) rendered"


def _backup() -> str:
    from app.automation.simulated_devices import run_simulated_backup
    from app.utils import db_session

    with db_session() as session:
        results = run_simulated_backup(session=session)
    failed = sorted(h for h, r in results.items() if not r.ok)
    detail = f"{len(results) - len(failed)}/{len(results)} simulated device(s) fetched"
    if failed:
        detail += f", unreachable: {', '.join(failed)}"
    return detail


def _compliance() -> str:
    from app.web.utils import run_compliance

    summary = run_compliance()
    return (
        f"{summary.compliant_count} compliant, {summary.non_compliant_count} non-compliant, "
        f"{len(summary.failed_fetch)} fetch failure(s)"
    )


def _inventory() -> str:
    from app.automation.inventory import build_inventory
    from app.utils import db_session

    with db_session() as session:
        result = build_inventory(session=session)
    return f"{len(result.hosts)} host(s), roles {', '.join(result.roles)}"


def main() -> int:
    _step("alembic upgrade head", _alembic_upgrade)
    _step("ensure_schema", _ensure_schema)
    _step("seed admin", _seed_admin)
    _step("run_pipeline", _run_pipeline)
    _step("activate devices", _activate_devices)
    _step("render_all_to_disk", _render)
    _step("run_simulated_backup", _backup)
    _step("run_compliance", _compliance)
    _step("build_inventory", _inventory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
