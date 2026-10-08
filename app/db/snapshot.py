"""Consistent point-in-time copies of a live SQLite database.

A plain filesystem copy (``shutil.copy2`` / ``Path.read_bytes``) of a SQLite
database is unsafe once WAL is enabled — which this app now enables (see
:mod:`app.db.engine`). Freshly committed rows can still live in the ``-wal``
sidecar until a checkpoint folds them into the main file, so copying only the
main ``.db`` captures a *stale* snapshot, and copying while a writer is mid
transaction can capture torn pages. Neither carries the ``-wal`` / ``-shm``
sidecars.

SQLite's online backup API sidesteps all of that: it reads a transactionally
consistent view of the source (WAL contents included) and writes a single,
checkpointed database file with no sidecars — exactly what the pipeline
snapshots and the download bundle want.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path


def copy_sqlite_consistent(src: Path, dst: Path) -> None:
    """Copy the SQLite database at *src* to *dst* as a consistent snapshot.

    Uses the SQLite backup API rather than a byte copy, so the result is safe
    under WAL. *dst* (and any missing parent directories) is created/overwritten.
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(src.as_posix())
    try:
        dest = sqlite3.connect(dst.as_posix())
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def snapshot_sqlite_bytes(src: Path) -> bytes:
    """Return the bytes of a consistent snapshot of the SQLite DB at *src*.

    Convenience for callers that need the snapshot in memory (e.g. to place it
    into a ZIP) rather than at a destination path. The snapshot is materialised
    to a temporary file — required because the backup API writes to a real
    database file — which is removed before returning.
    """
    src = Path(src)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "snapshot.db"
        copy_sqlite_consistent(src, tmp_db)
        return tmp_db.read_bytes()
