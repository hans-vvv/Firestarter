"""Creates and manages point-in-time snapshots of the Excel workbook."""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from app.db.snapshot import copy_sqlite_consistent
from app.domain.file_locations import (
    ADDRESSING_DEF_LOC,
    COMPLIANCE_EXTRA_LOC,
    COMPLIANCE_IGNORE_LOC,
    COMPLIANCE_REMEDIATION_LOC,
    PRODUCTION_DB_LOC,
    SERVICES_DEF_LOC,
    TOPOLOGY_EXCEL_LOC,
)

WB_NAME = TOPOLOGY_EXCEL_LOC.path
DB_NAME = PRODUCTION_DB_LOC.path

# How many timestamped snapshots to keep per category. Every successful run
# archives the previous ``latest`` into ``history/`` and every failed run drops
# one into ``failed/``, so both directories accumulate without bound unless pruned.
# ``latest`` is never a candidate. This is the quick-change knob — bump it here and
# nothing else needs touching. Mirrors ``MAX_BACKUPS`` in ``app/automation/backup.py``.
MAX_SNAPSHOTS = 20

log = logging.getLogger(__name__)

# Reference-input dirs captured alongside every snapshot, keyed by the sub-folder
# they land in; each value is (source location, include globs). Archiving the live
# policy inputs AND the compliance reference data makes a snapshot a complete record
# of what produced a given DB/Excel state and what the compliance gate compared it
# against. The globs mirror the download-bundle taxonomy (app/data_bundle/spec.py):
# ``ignore/``'s ``__init__.py`` package marker is naturally skipped by ``*.yaml``.
_DEFINITION_DIRS: dict[str, tuple[Path, tuple[str, ...]]] = {
    "service_definitions": (SERVICES_DEF_LOC.path, ("*.yaml", "*.yml")),
    "addressing_definitions": (ADDRESSING_DEF_LOC.path, ("*.yaml", "*.yml")),
    "compliance_extra": (COMPLIANCE_EXTRA_LOC.path, ("*.cfg",)),
    "compliance_ignore": (COMPLIANCE_IGNORE_LOC.path, ("*.yaml", "*.yml")),
    # Per-role remediation specs (ADR 0004). The gitignored ``<role>.yaml`` files
    # are the per-environment input; the package's tracked ``__init__.py``,
    # ``README.md`` and ``engine.py`` are naturally skipped by the ``*.yaml`` glob.
    "compliance_remediation": (COMPLIANCE_REMEDIATION_LOC.path, ("*.yaml", "*.yml")),
}


def _copy_definitions(dest: Path) -> None:
    """Copy the live reference-input files into ``dest`` sub-folders.

    Only top-level files matching each dir's include globs are captured — the
    ``backup/`` subdir, package markers and any stray non-input files are skipped,
    so the archive holds exactly the policy + compliance inputs a run consumed.
    """
    for subdir, (src, globs) in _DEFINITION_DIRS.items():
        if not src.is_dir():
            continue
        out = dest / subdir
        out.mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            if f.is_file() and any(f.match(g) for g in globs):
                shutil.copy2(f, out / f.name)


def _snapshot_paths():
    """
    Resolve the source workbook path, source database path, and snapshot base directory.

    Paths are resolved under the live data root (repo root by default,
    ``FIRESTARTER_DATA`` when set). The snapshot base directory is created under
    the database file's parent directory as ``state_snapshots``.

    Returns
    -------
    tuple[pathlib.Path, pathlib.Path, pathlib.Path]
        A tuple containing:
        - path to the source Excel workbook
        - path to the source database
        - path to the snapshot base directory
    """
    excel_src = WB_NAME
    db_src = DB_NAME

    base = db_src.parent / "state_snapshots"
    return excel_src, db_src, base


def prune_snapshots(directory: Path, *, keep: int = MAX_SNAPSHOTS) -> list[Path]:
    """Delete timestamped snapshots in *directory* beyond the newest *keep*.

    A snapshot is any sub-directory of *directory*, named for its UTC collection
    timestamp (``YYYY-MM-DD_HH-MM-SS``), which sorts lexicographically in
    chronological order — so the newest *keep* are simply the tail of the sorted
    list and everything before it is old enough to drop. Returns the removed paths.

    A no-op when *directory* is absent or holds at most *keep* snapshots. Stray
    files (never created here) are ignored. Mirrors
    :func:`app.automation.backup.prune_old_backups`.
    """
    if not directory.is_dir():
        return []

    snaps = sorted(p for p in directory.iterdir() if p.is_dir())
    if len(snaps) <= keep:
        return []

    to_remove = snaps[: len(snaps) - keep]
    for path in to_remove:
        log.info("pruning old snapshot %s", path)
        shutil.rmtree(path)
    return to_remove


def snapshot_latest() -> None:
    """
    Save the current workbook and database as the latest successful snapshot.

    The snapshot layout is:

    - ``state_snapshots/latest`` for the most recent successful state
    - ``state_snapshots/history/<timestamp>`` for the previously stored latest state

    If a latest snapshot already exists, it is first archived into the history
    directory using a UTC timestamp. The current workbook, database and YAML
    definition files are then copied into ``latest``. Afterwards ``history/`` is
    pruned to the newest :data:`MAX_SNAPSHOTS` entries so it cannot grow without
    bound.

    Returns
    -------
    None
    """
    excel_src, db_src, base = _snapshot_paths()

    latest = base / "latest"
    history = base / "history"
    latest.mkdir(parents=True, exist_ok=True)
    history.mkdir(parents=True, exist_ok=True)

    # archive current latest (if present)
    has_existing = (latest / excel_src.name).exists() or (latest / db_src.name).exists()
    if has_existing:
        ts = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
        arch = history / ts
        arch.mkdir(parents=True, exist_ok=True)

        for fname in (excel_src.name, db_src.name):
            p = latest / fname
            if p.exists():
                shutil.move(str(p), str(arch / fname))
        # The definition sub-folders move with the Excel/DB so each history
        # entry is a complete, self-contained snapshot.
        for subdir in _DEFINITION_DIRS:
            p = latest / subdir
            if p.exists():
                shutil.move(str(p), str(arch / subdir))

    # write new latest. The DB goes through the SQLite backup API (WAL-safe);
    # the Excel workbook is a plain file so an ordinary copy is correct.
    shutil.copy2(excel_src, latest / excel_src.name)
    copy_sqlite_consistent(db_src, latest / db_src.name)
    _copy_definitions(latest)

    prune_snapshots(history)


def snapshot_failed() -> None:
    """
    Save the current workbook and database as a failed-run snapshot.

    Failed snapshots are stored under
    ``state_snapshots/failed/<timestamp>`` to preserve the exact input,
    database state and YAML definitions associated with an unsuccessful run for
    later troubleshooting. Afterwards ``failed/`` is pruned to the newest
    :data:`MAX_SNAPSHOTS` entries so it cannot grow without bound.

    Returns
    -------
    None
    """
    excel_src, db_src, base = _snapshot_paths()

    ts = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    dst = base / "failed" / ts
    dst.mkdir(parents=True, exist_ok=True)

    shutil.copy2(excel_src, dst / excel_src.name)
    copy_sqlite_consistent(db_src, dst / db_src.name)
    _copy_definitions(dst)

    prune_snapshots(base / "failed")
