"""Seed a fresh worktree with the untracked production-data files.

Production data (the database, the input workbook, the production YAML) is
gitignored — it travels between environments via the data-bundle download, not
git (see :mod:`app.data_bundle.spec`). A consequence: a newly created git worktree
does NOT receive those files via checkout, so the pipeline and the mandatory
compliance-snapshot baseline would have no database to run against.

This copies the bundle-owned files from a source **data root** (normally the
deployed instance's data root, e.g. ``<data-root>``) into the current
worktree's data root, using the same spec that drives the download so the two
never drift. The point is to fill in the gitignored environment data a fresh
worktree lacks.

Usage, from the worktree root::

    python -m app.data_bundle.seed_worktree <source-data-root>
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from app.data_bundle.spec import resolve_bundle_files
from app.domain.file_locations import data_root


def seed_worktree(*, source_root: Path, dest_root: Path) -> list[str]:
    """Copy every bundle-owned file from *source_root* into *dest_root*.

    Both are data roots. Returns the data-root-relative paths copied, in
    deterministic order. Skips files missing at the source (a source that never
    had a workbook simply seeds none).
    """
    copied: list[str] = []
    for rel in resolve_bundle_files(data_root=source_root):
        src = source_root / rel
        if not src.is_file():
            continue
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied.append(rel.as_posix())
    return copied


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: python -m app.data_bundle.seed_worktree <source-data-root>",
            file=sys.stderr,
        )
        return 2
    source = Path(argv[1]).resolve()
    if not source.is_dir():
        print(f"source data root does not exist: {source}", file=sys.stderr)
        return 2
    dest = data_root()
    copied = seed_worktree(source_root=source, dest_root=dest)
    print(f"Seeded {len(copied)} production-data file(s) from {source} into {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
