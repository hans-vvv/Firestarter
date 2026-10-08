"""Declares which files are *environment data* — the download-bundle contents.

The project splits files in one place, along one line:

* **Code** — Python, templates, and the suite's own fixtures under ``tests/``.
  Version-controlled; travels between environments via git branch/merge.
* **Environment data** — the database, the input workbook, the compliance
  reference/ignore files, and *all* the service/addressing YAML. Its values
  differ per server, so it is pulled out as a downloadable ZIP.

There is deliberately **no lab-vs-production distinction in this module**. Each
server exports its own dataset: the test server's bundle carries lab values, the
production server's carries production values. Which one you have is simply a
function of where you downloaded it from, not of a filename convention. (The test
suite owns its own fixtures under ``tests/fixtures/`` and reads none of these
files — verified by running the suite with every YAML absent.)

This module is the single source of truth for that data set. The web layer never
hard-codes paths — it asks :func:`resolve_bundle_files` what a bundle contains, so
the taxonomy lives in exactly one place.

The bundle is download-only, so this list only ever drives *reading* files into a
ZIP — it never deletes or overwrites anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.domain.file_locations import (
    ADDRESSING_DEF_LOC,
    BACKUPS_LOC,
    COMPLIANCE_EXTRA_LOC,
    COMPLIANCE_IGNORE_LOC,
    COMPLIANCE_REMEDIATION_LOC,
    PRODUCTION_DB_LOC,
    SERVICES_DEF_LOC,
    SIMULATION_LOC,
    TOPOLOGY_EXCEL_LOC,
)


@dataclass(frozen=True)
class BundleFile:
    """A single production-data file, path relative to the data root.

    Used for the singletons: the database and the input workbook.
    """

    path: str


@dataclass(frozen=True)
class BundleDir:
    """A directory whose files are selected by glob.

    ``include`` globs are evaluated **relative to** ``base``; a file is included
    when it matches any of them. Only files directly in ``base`` are considered
    (no recursion into subdirectories), which keeps any nested ``backup/`` folder
    out of the bundle.
    """

    base: str
    include: tuple[str, ...]


# ---------------------------------------------------------------------------
# The taxonomy. Paths come from the single registry (app.domain.file_locations)
# so the bundle/seed path and the runtime path can never describe different files.
# ---------------------------------------------------------------------------

BUNDLE_FILES: tuple[BundleFile, ...] = (
    BundleFile(PRODUCTION_DB_LOC.location),
    BundleFile(TOPOLOGY_EXCEL_LOC.location),
)

BUNDLE_DIRS: tuple[BundleDir, ...] = (
    # Compliance reference configs and ignore rules.
    BundleDir(COMPLIANCE_EXTRA_LOC.location, include=("*.cfg",)),
    BundleDir(COMPLIANCE_IGNORE_LOC.location, include=("*.yaml",)),
    # Per-role remediation specs (ADR 0004).
    BundleDir(COMPLIANCE_REMEDIATION_LOC.location, include=("*.yaml",)),
    # Drift the simulated devices show (app/automation/simulated_devices.py).
    BundleDir(SIMULATION_LOC.location, include=("*.yaml",)),
    # Every service definition on this server — whatever tenant/environment it
    # describes. No lab/production filtering: the server you download from decides
    # which values you get.
    BundleDir(SERVICES_DEF_LOC.location, include=("*_def.yaml",)),
    # Every addressing policy on this server, same reasoning.
    BundleDir(ADDRESSING_DEF_LOC.location, include=("*.yaml",)),
    # The most recent device config backup — the *live* side of every compliance
    # run. Without it a seeded checkout renders configs but has nothing to compare
    # them against, so the compliance gate quietly degrades to "everything
    # skipped". Only ``latest/`` travels: the timestamped archives beside it are
    # local history and would grow the bundle without bound.
    BundleDir(
        f"{BACKUPS_LOC.location}/latest", include=("*.cfg", "fetch_results.json", "datetime.txt")
    ),
)


def _matches(rel: Path, globs: tuple[str, ...]) -> bool:
    """True if *rel*'s final component matches any of *globs*."""
    return any(rel.match(g) for g in globs)


def resolve_dir_files(spec: BundleDir, *, data_root: Path) -> list[Path]:
    """Return the selected files of a :class:`BundleDir`, as data-root-relative paths.

    Missing directories yield an empty list — a fresh environment that has never
    had, say, an addressing policy simply contributes nothing rather than
    erroring. Results are sorted for deterministic bundle contents.
    """
    base = data_root / spec.base
    if not base.is_dir():
        return []
    owned = [
        entry.relative_to(data_root)
        for entry in base.iterdir()
        if entry.is_file() and _matches(Path(entry.name), spec.include)
    ]
    return sorted(owned, key=lambda p: p.as_posix())


def resolve_bundle_files(*, data_root: Path) -> list[Path]:
    """Return every production-data file present under *data_root*, data-root-relative.

    Singleton files that do not exist are skipped (a bundle built on a fresh box
    without a workbook yet simply omits it). Directory contents come from
    :func:`resolve_dir_files`. The order is deterministic (singletons first in
    declaration order, then each directory's sorted contents).
    """
    files: list[Path] = []
    for bf in BUNDLE_FILES:
        if (data_root / bf.path).is_file():
            files.append(Path(bf.path))
    for bd in BUNDLE_DIRS:
        files.extend(resolve_dir_files(bd, data_root=data_root))
    return files
