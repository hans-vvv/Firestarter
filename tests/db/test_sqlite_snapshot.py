"""WAL-safety of :mod:`app.db.snapshot`.

The reason these helpers exist: under WAL a plain byte copy of the main ``.db``
file can miss rows that are committed but still living in the ``-wal`` sidecar.
The backup API must capture them.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from app.db.snapshot import copy_sqlite_consistent, snapshot_sqlite_bytes


def test_backup_captures_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    src = tmp_path / "live.db"
    conn = sqlite3.connect(src.as_posix(), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (id, v) VALUES (1, 'in-wal')")
    # Row is committed (autocommit) but, with autocheckpoint off, still in -wal.
    assert (tmp_path / "live.db-wal").exists()

    dst = tmp_path / "snap.db"
    copy_sqlite_consistent(src, dst)

    conn.close()

    # The backup captured the WAL row...
    snap = sqlite3.connect(dst.as_posix())
    try:
        assert snap.execute("SELECT v FROM t WHERE id = 1").fetchone() == ("in-wal",)
    finally:
        snap.close()
    # ...and produced a single self-contained file (no sidecars to carry).
    assert not (tmp_path / "snap.db-wal").exists()


def test_naive_copy_would_miss_the_wal_row(tmp_path: Path) -> None:
    """Contrast case: a plain file copy of just the main .db loses the row,
    which is exactly the bug the helper avoids."""
    src = tmp_path / "live.db"
    conn = sqlite3.connect(src.as_posix(), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (id, v) VALUES (1, 'in-wal')")

    naive = tmp_path / "naive.db"
    shutil.copyfile(src, naive)  # main file only — the -wal is left behind
    conn.close()

    got = sqlite3.connect(naive.as_posix())
    try:
        try:
            # With autocheckpoint off, everything since WAL mode was set — even
            # the CREATE TABLE — sits in the sidecar, so the naive copy has
            # neither the table nor the row.
            row = got.execute("SELECT v FROM t WHERE id = 1").fetchone()
        except sqlite3.OperationalError:
            row = None
        assert row is None
    finally:
        got.close()


def test_snapshot_sqlite_bytes_roundtrips(tmp_path: Path) -> None:
    src = tmp_path / "live.db"
    conn = sqlite3.connect(src.as_posix())
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()

    data = snapshot_sqlite_bytes(src)

    out = tmp_path / "out.db"
    out.write_bytes(data)
    conn = sqlite3.connect(out.as_posix())
    try:
        assert conn.execute("SELECT v FROM t").fetchone() == ("hello",)
    finally:
        conn.close()
