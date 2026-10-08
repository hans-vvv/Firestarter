from __future__ import annotations

"""Tests for ``app.excel_data_handling.snapshots`` — in particular that every
snapshot also archives the live YAML definition files in their own sub-folders.

``_snapshot_paths`` is monkeypatched to a temp layout and ``_DEFINITION_DIRS`` is
repointed at temp source dirs (absolute paths, so the module's ``repo_root /
loc`` join resolves straight to them) so nothing touches the real repo.
"""

import os
import sqlite3
from pathlib import Path

import pytest

import app.excel_data_handling.snapshots as snapshots
from app.config import get_database_url


def _write_sqlite_db(path: Path, marker: str) -> None:
    """Create a minimal but real SQLite database at *path* holding *marker*.

    The DB copy now goes through the SQLite backup API, so the source must be an
    actual database file, not arbitrary bytes.
    """
    conn = sqlite3.connect(path.as_posix())
    try:
        conn.execute("CREATE TABLE marker (v TEXT)")
        conn.execute("INSERT INTO marker (v) VALUES (?)", (marker,))
        conn.commit()
    finally:
        conn.close()


def _read_sqlite_marker(path: Path) -> str:
    conn = sqlite3.connect(path.as_posix())
    try:
        return conn.execute("SELECT v FROM marker").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.skipif(
    "DATABASE_URL" in os.environ,
    reason=(
        "The invariant is about the default, repo-root database. A DATABASE_URL "
        "override moves the engine but not the snapshot path, and is expected to."
    ),
)
def test_snapshot_paths_point_at_the_database_the_engine_opens():
    """The unpatched ``_snapshot_paths`` must resolve to the live database file.

    Every other test in this module monkeypatches ``_snapshot_paths``, so the
    real ``PRODUCTION_DB_LOC`` constant is never exercised by them.  That let a
    casing mismatch (``app.DB`` vs ``app.db``) survive undetected: it resolved
    fine on the Windows laptop's case-insensitive filesystem and only failed
    once the pipeline ran on the Linux server, mid-run and after ``wipe_db()``.

    Comparing against the engine's own URL rather than a hardcoded literal
    keeps the two definitions of "the database" from drifting apart again.
    """
    _, db_src, _ = snapshots._snapshot_paths()

    engine_db = Path(get_database_url().removeprefix("sqlite:///"))

    assert db_src == engine_db


@pytest.fixture
def layout(tmp_path: Path, monkeypatch):
    """Build temp Excel/DB sources, temp def dirs, and a temp snapshot base."""
    excel_src = tmp_path / "topology.xlsx"
    db_src = tmp_path / "app.db"
    excel_src.write_text("EXCEL")
    _write_sqlite_db(db_src, "DB")
    base = tmp_path / "state_snapshots"

    svc = tmp_path / "svc_defs"
    addr = tmp_path / "addr_defs"
    svc.mkdir()
    addr.mkdir()
    (svc / "vprn_def.yaml").write_text("a: 1")
    (svc / "extra.yml").write_text("b: 2")
    (svc / "README.txt").write_text("not a def")  # skipped (wrong suffix)
    (svc / "backup").mkdir()
    (svc / "backup" / "old_def.yaml").write_text("c: 3")  # skipped (subdir)
    (addr / "addressing_lab_def.yaml").write_text("d: 4")

    # Compliance reference inputs: extra/ (*.cfg), ignore/ (*.yaml) and the
    # per-role remediation/ specs (*.yaml).
    extra = tmp_path / "compliance_extra"
    ignore = tmp_path / "compliance_ignore"
    remediation = tmp_path / "compliance_remediation"
    extra.mkdir()
    ignore.mkdir()
    remediation.mkdir()
    (extra / "rr1.tst-001.cfg").write_text("cfg-a")
    (extra / "core1.tst-001.cfg").write_text("cfg-b")
    (extra / "notes.txt").write_text("skip me")  # skipped (wrong suffix)
    (ignore / "Base.yaml").write_text("rules: 1")
    (ignore / "__init__.py").write_text("")  # skipped (package marker)
    (remediation / "pe.yaml").write_text("add: []")
    (remediation / "README.md").write_text("not a spec")  # skipped (wrong suffix)
    (remediation / "__init__.py").write_text("")  # skipped (package marker)

    monkeypatch.setattr(snapshots, "_snapshot_paths", lambda: (excel_src, db_src, base))
    monkeypatch.setattr(
        snapshots,
        "_DEFINITION_DIRS",
        {
            "service_definitions": (svc, ("*.yaml", "*.yml")),
            "addressing_definitions": (addr, ("*.yaml", "*.yml")),
            "compliance_extra": (extra, ("*.cfg",)),
            "compliance_ignore": (ignore, ("*.yaml", "*.yml")),
            "compliance_remediation": (remediation, ("*.yaml", "*.yml")),
        },
    )
    return base


def test_latest_archives_only_top_level_yaml_defs(layout):
    snapshots.snapshot_latest()

    svc_out = layout / "latest" / "service_definitions"
    addr_out = layout / "latest" / "addressing_definitions"

    assert {p.name for p in svc_out.iterdir()} == {"vprn_def.yaml", "extra.yml"}
    assert (addr_out / "addressing_lab_def.yaml").exists()
    # .txt and the backup/ subdir are excluded.
    assert not (svc_out / "README.txt").exists()
    assert not (svc_out / "backup").exists()


def test_latest_captures_compliance_extra_and_ignore(layout):
    snapshots.snapshot_latest()

    extra_out = layout / "latest" / "compliance_extra"
    ignore_out = layout / "latest" / "compliance_ignore"
    remediation_out = layout / "latest" / "compliance_remediation"

    # extra/ captures *.cfg only; ignore/ captures *.yaml (not __init__.py).
    assert {p.name for p in extra_out.iterdir()} == {"rr1.tst-001.cfg", "core1.tst-001.cfg"}
    assert {p.name for p in ignore_out.iterdir()} == {"Base.yaml"}
    assert not (extra_out / "notes.txt").exists()  # wrong suffix
    assert not (ignore_out / "__init__.py").exists()  # package marker skipped
    # remediation/ captures *.yaml only (not README.md or __init__.py).
    assert {p.name for p in remediation_out.iterdir()} == {"pe.yaml"}
    assert not (remediation_out / "README.md").exists()  # wrong suffix
    assert not (remediation_out / "__init__.py").exists()  # package marker skipped


def test_latest_copies_excel_and_db_alongside_defs(layout):
    snapshots.snapshot_latest()
    latest = layout / "latest"
    assert (latest / "topology.xlsx").read_text() == "EXCEL"
    # The DB snapshot is a real SQLite copy carrying the source's row.
    assert _read_sqlite_marker(latest / "app.db") == "DB"


def test_second_latest_rolls_previous_into_history_with_defs(layout):
    snapshots.snapshot_latest()
    snapshots.snapshot_latest()

    history = layout / "history"
    archived = list(history.iterdir())
    assert len(archived) == 1
    arch = archived[0]
    # The whole previous snapshot — Excel, DB and the def sub-folders — moved.
    assert (arch / "topology.xlsx").exists()
    assert (arch / "app.db").exists()
    assert (arch / "service_definitions" / "vprn_def.yaml").exists()
    assert (arch / "addressing_definitions" / "addressing_lab_def.yaml").exists()
    # Compliance reference inputs move with the rest into history.
    assert (arch / "compliance_extra" / "rr1.tst-001.cfg").exists()
    assert (arch / "compliance_ignore" / "Base.yaml").exists()
    assert (arch / "compliance_remediation" / "pe.yaml").exists()
    # latest still holds a fresh full copy.
    assert (layout / "latest" / "service_definitions" / "vprn_def.yaml").exists()
    assert (layout / "latest" / "compliance_extra" / "rr1.tst-001.cfg").exists()
    assert (layout / "latest" / "compliance_remediation" / "pe.yaml").exists()


def test_failed_snapshot_includes_defs(layout):
    snapshots.snapshot_failed()

    failed_runs = list((layout / "failed").iterdir())
    assert len(failed_runs) == 1
    dst = failed_runs[0]
    assert (dst / "topology.xlsx").exists()
    assert (dst / "app.db").exists()
    assert (dst / "service_definitions" / "vprn_def.yaml").exists()
    assert (dst / "addressing_definitions" / "addressing_lab_def.yaml").exists()
    assert (dst / "compliance_extra" / "rr1.tst-001.cfg").exists()
    assert (dst / "compliance_ignore" / "Base.yaml").exists()
    assert (dst / "compliance_remediation" / "pe.yaml").exists()


class TestPruneSnapshots:
    """Retention: history/ and failed/ keep only the newest MAX_SNAPSHOTS entries."""

    def _make_snapshots(self, directory: Path, names):
        for name in names:
            d = directory / name
            d.mkdir(parents=True)
            (d / "app.db").write_text("DB", encoding="utf-8")

    def test_noop_when_at_or_under_limit(self, tmp_path):
        names = [f"2026-08-{d:02d}_00-00-00" for d in range(1, 4)]
        self._make_snapshots(tmp_path, names)
        removed = snapshots.prune_snapshots(tmp_path, keep=3)
        assert removed == []
        assert sorted(p.name for p in tmp_path.iterdir()) == names

    def test_removes_only_the_oldest_beyond_keep(self, tmp_path):
        # Ten snapshots, keep 3 → the seven oldest go, the three newest stay.
        names = [f"2026-08-{d:02d}_00-00-00" for d in range(1, 11)]
        self._make_snapshots(tmp_path, names)
        removed = snapshots.prune_snapshots(tmp_path, keep=3)

        assert sorted(p.name for p in removed) == names[:7]
        assert sorted(p.name for p in tmp_path.iterdir()) == names[7:]

    def test_missing_dir_is_a_noop(self, tmp_path):
        assert snapshots.prune_snapshots(tmp_path / "absent", keep=2) == []

    def test_stray_files_are_ignored(self, tmp_path):
        (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
        names = [f"2026-08-{d:02d}_00-00-00" for d in range(1, 4)]
        self._make_snapshots(tmp_path, names)
        removed = snapshots.prune_snapshots(tmp_path, keep=1)

        assert sorted(p.name for p in removed) == names[:2]
        assert (tmp_path / "notes.txt").exists()  # the stray file is untouched
        assert sorted(p.name for p in tmp_path.iterdir() if p.is_dir()) == names[2:]

    def test_default_keep_is_max_snapshots(self):
        assert snapshots.MAX_SNAPSHOTS == 20


class TestSnapshotWiresInPruning:
    """snapshot_latest/failed prune the correct directory after writing."""

    def test_latest_prunes_history(self, layout, monkeypatch):
        calls: list[Path] = []
        monkeypatch.setattr(snapshots, "prune_snapshots", lambda d, **kw: calls.append(d) or [])
        snapshots.snapshot_latest()
        assert calls == [layout / "history"]

    def test_failed_prunes_failed(self, layout, monkeypatch):
        calls: list[Path] = []
        monkeypatch.setattr(snapshots, "prune_snapshots", lambda d, **kw: calls.append(d) or [])
        snapshots.snapshot_failed()
        assert calls == [layout / "failed"]
