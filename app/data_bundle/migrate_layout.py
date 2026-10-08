"""One-time migration: the pre-flatten ``app/...`` data layout → the flat data root.

Sprint B moves every per-environment data file out of the ``app/`` package into a
single data root (``FIRESTARTER_DATA``, default ``<repo>/data``) with a flat,
code-free layout. This module copies an existing install's data from the old
locations to the new ones, so a deployed instance or a dev checkout can be
migrated in place.

Design notes
------------
* **Old paths are hardcoded here on purpose.** After this sprint they no longer
  exist in :mod:`app.domain.file_locations`; a one-time migration is the one place
  it is correct to name them literally.
* **The same include-globs as the bundle taxonomy** (:mod:`app.data_bundle.spec`)
  select the data and leave code siblings behind — ``__init__.py``, ``engine.py``,
  ``README.md``, the ``services_definitions/backup/`` subdir — without enumerating
  them. ``tests/test_migrate_layout.py`` pins that the new targets match the
  registry, so the two cannot drift.
* **Copy, never move.** The old tree is left intact so the migration is
  reversible; delete it only after verifying the instance is healthy on the new
  root. Re-running is safe (idempotent overwrite).
* The database is copied with :func:`app.db.snapshot.copy_sqlite_consistent`
  (SQLite backup API), so it is consistent under WAL and lands as a single file
  with no ``-wal`` / ``-shm`` sidecars to carry.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.db.snapshot import copy_sqlite_consistent


@dataclass(frozen=True)
class Move:
    """One migration entry: an ``old`` path (relative to the source root) copied to
    a ``new`` path (relative to the destination data root).

    ``globs`` empty → ``old`` is a single file. Otherwise ``old`` is a directory
    and only its top-level entries matching a glob are copied (no recursion, so
    nested ``backup/`` folders and package markers are skipped).
    """

    old: str
    new: str
    globs: tuple[str, ...] = field(default_factory=tuple)
    sqlite: bool = False


# The taxonomy, mirroring app/data_bundle/spec.py's includes exactly.
MOVES: tuple[Move, ...] = (
    Move(old="app.db", new="app.db", sqlite=True),
    Move(old="app/topology.xlsx", new="topology.xlsx"),
    Move(
        old="app/services/services_definitions",
        new="services/definitions",
        globs=("*_def.yaml",),
    ),
    Move(
        old="app/services/service_handling/addressing_definitions",
        new="services/addressing",
        globs=("*.yaml",),
    ),
    Move(old="app/compliance/ignore", new="compliance/ignore", globs=("*.yaml",)),
    Move(old="app/compliance/extra", new="compliance/extra", globs=("*.cfg",)),
    Move(old="app/compliance/remediation", new="compliance/remediation", globs=("*.yaml",)),
    Move(
        old="app/backups/latest",
        new="backups/latest",
        globs=("*.cfg", "fetch_results.json", "datetime.txt"),
    ),
)


def _copy_file(src: Path, dst: Path, *, sqlite: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if sqlite:
        copy_sqlite_consistent(src, dst)
    else:
        shutil.copy2(src, dst)


def _copy_dir(src: Path, dst: Path, globs: tuple[str, ...]) -> int:
    """Copy top-level files of *src* matching *globs* into *dst*. Returns the count."""
    copied = 0
    for entry in sorted(src.iterdir(), key=lambda p: p.name):
        if entry.is_file() and any(entry.match(g) for g in globs):
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, dst / entry.name)
            copied += 1
    return copied


def migrate(*, src_root: Path, dst_root: Path) -> list[str]:
    """Copy every data file from the old layout under *src_root* to the flat layout
    under *dst_root*. Missing sources are skipped. Returns a human-readable log.
    """
    src_root = Path(src_root)
    dst_root = Path(dst_root)
    log: list[str] = []

    for mv in MOVES:
        src = src_root / mv.old
        dst = dst_root / mv.new
        if mv.globs:
            if not src.is_dir():
                log.append(f"skip  {mv.old} (no such dir)")
                continue
            n = _copy_dir(src, dst, mv.globs)
            log.append(f"copy  {mv.old}/  →  {mv.new}/   ({n} file(s))")
        else:
            if not src.is_file():
                log.append(f"skip  {mv.old} (no such file)")
                continue
            _copy_file(src, dst, sqlite=mv.sqlite)
            log.append(
                f"copy  {mv.old}  →  {mv.new}" + ("  (sqlite-consistent)" if mv.sqlite else "")
            )
    return log


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src_root", help="Existing install root (old nested layout)")
    parser.add_argument("dst_root", help="New data root (flat layout)")
    args = parser.parse_args(argv)

    log = migrate(src_root=Path(args.src_root).resolve(), dst_root=Path(args.dst_root).resolve())
    for line in log:
        print(line)
    print(f"\nMigrated into {args.dst_root}. Old tree left intact — verify, then remove it.")


if __name__ == "__main__":
    sys.exit(main())
