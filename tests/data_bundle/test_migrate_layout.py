"""The one-time pre-flatten → flat data-root migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.data_bundle import migrate_layout
from app.data_bundle.migrate_layout import MOVES, migrate
from app.domain.file_locations import (
    ADDRESSING_DEF_LOC,
    BACKUPS_LOC,
    COMPLIANCE_EXTRA_LOC,
    COMPLIANCE_IGNORE_LOC,
    COMPLIANCE_REMEDIATION_LOC,
    PRODUCTION_DB_LOC,
    SERVICES_DEF_LOC,
    TOPOLOGY_EXCEL_LOC,
)


def _make_old_tree(root: Path) -> None:
    """A pre-Sprint-B install: data interleaved with code under app/."""
    conn = sqlite3.connect(root / "app.db")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('hello')")
    conn.commit()
    conn.close()

    (root / "app").mkdir()
    (root / "app" / "topology.xlsx").write_bytes(b"XLSX")

    sd = root / "app" / "services" / "services_definitions"
    sd.mkdir(parents=True)
    (sd / "bgp_def.yaml").write_text("def")
    (sd / "notes.yaml").write_text("not a def")  # not *_def.yaml → stays
    (sd / "backup").mkdir()
    (sd / "backup" / "old_def.yaml").write_text("old")  # subdir → stays

    ad = root / "app" / "services" / "service_handling" / "addressing_definitions"
    ad.mkdir(parents=True)
    (ad / "addressing_lab_def.yaml").write_text("addr")

    ig = root / "app" / "compliance" / "ignore"
    ig.mkdir(parents=True)
    (ig / "base.yaml").write_text("rules")
    (ig / "__init__.py").write_text("")  # code → stays

    ex = root / "app" / "compliance" / "extra"
    ex.mkdir(parents=True)
    (ex / "rr1.tst-001.cfg").write_text("cfg")

    rem = root / "app" / "compliance" / "remediation"
    rem.mkdir(parents=True)
    (rem / "pe.yaml").write_text("allow")
    (rem / "engine.py").write_text("code")  # code → stays

    lat = root / "app" / "backups" / "latest"
    lat.mkdir(parents=True)
    (lat / "core1.cfg").write_text("live")
    (lat / "datetime.txt").write_text("2026")


def test_migration_produces_the_flat_layout(tmp_path: Path) -> None:
    src, dst = tmp_path / "install", tmp_path / "data"
    src.mkdir()
    _make_old_tree(src)

    migrate(src_root=src, dst_root=dst)

    assert (dst / "app.db").is_file()
    assert (dst / "topology.xlsx").read_bytes() == b"XLSX"
    assert (dst / "services/definitions/bgp_def.yaml").read_text() == "def"
    assert (dst / "services/addressing/addressing_lab_def.yaml").read_text() == "addr"
    assert (dst / "compliance/ignore/base.yaml").read_text() == "rules"
    assert (dst / "compliance/extra/rr1.tst-001.cfg").read_text() == "cfg"
    assert (dst / "compliance/remediation/pe.yaml").read_text() == "allow"
    assert (dst / "backups/latest/core1.cfg").read_text() == "live"
    assert (dst / "backups/latest/datetime.txt").read_text() == "2026"


def test_migration_leaves_code_siblings_and_strays_behind(tmp_path: Path) -> None:
    src, dst = tmp_path / "install", tmp_path / "data"
    src.mkdir()
    _make_old_tree(src)

    migrate(src_root=src, dst_root=dst)

    # Code siblings and non-matching strays must NOT be copied.
    assert not (dst / "compliance/ignore/__init__.py").exists()
    assert not (dst / "compliance/remediation/engine.py").exists()
    assert not (dst / "services/definitions/notes.yaml").exists()  # not *_def.yaml
    assert not (dst / "services/definitions/backup").exists()  # subdir not recursed


def test_database_is_copied_consistently(tmp_path: Path) -> None:
    src, dst = tmp_path / "install", tmp_path / "data"
    src.mkdir()
    _make_old_tree(src)

    migrate(src_root=src, dst_root=dst)

    conn = sqlite3.connect(dst / "app.db")
    try:
        assert conn.execute("SELECT v FROM t").fetchone()[0] == "hello"
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    src, dst = tmp_path / "install", tmp_path / "data"
    src.mkdir()
    _make_old_tree(src)

    first = migrate(src_root=src, dst_root=dst)
    second = migrate(src_root=src, dst_root=dst)

    assert first == second
    assert (dst / "topology.xlsx").read_bytes() == b"XLSX"


def test_missing_sources_are_skipped(tmp_path: Path) -> None:
    src, dst = tmp_path / "install", tmp_path / "data"
    src.mkdir()
    # Only the workbook exists — everything else is absent.
    (src / "app").mkdir()
    (src / "app" / "topology.xlsx").write_bytes(b"X")

    log = migrate(src_root=src, dst_root=dst)

    assert (dst / "topology.xlsx").is_file()
    assert any("skip" in line and "app.db" in line for line in log)
    assert not (dst / "app.db").exists()


def test_move_targets_match_the_registry() -> None:
    """The migration's new paths must equal the live registry locations, so the
    flattened layout and the migration can never drift apart."""
    by_old = {m.old: m.new for m in MOVES}
    assert by_old["app.db"] == PRODUCTION_DB_LOC.location
    assert by_old["app/topology.xlsx"] == TOPOLOGY_EXCEL_LOC.location
    assert by_old["app/services/services_definitions"] == SERVICES_DEF_LOC.location
    assert (
        by_old["app/services/service_handling/addressing_definitions"]
        == ADDRESSING_DEF_LOC.location
    )
    assert by_old["app/compliance/ignore"] == COMPLIANCE_IGNORE_LOC.location
    assert by_old["app/compliance/extra"] == COMPLIANCE_EXTRA_LOC.location
    assert by_old["app/compliance/remediation"] == COMPLIANCE_REMEDIATION_LOC.location
    assert by_old["app/backups/latest"] == f"{BACKUPS_LOC.location}/latest"


def test_cli_entrypoint(tmp_path: Path, capsys) -> None:
    src, dst = tmp_path / "install", tmp_path / "data"
    src.mkdir()
    _make_old_tree(src)

    migrate_layout.main([str(src), str(dst)])

    out = capsys.readouterr().out
    assert "topology.xlsx" in out
    assert (dst / "topology.xlsx").is_file()
