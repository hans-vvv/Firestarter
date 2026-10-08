from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path

from app.data_bundle import core
from app.data_bundle.core import build_bundle

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_db(path: Path, *, revision: str | None = "0001") -> None:
    """Write a tiny valid SQLite DB, optionally stamped with an Alembic revision."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE thing (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO thing (val) VALUES ('hello')")
        if revision is not None:
            conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            conn.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        conn.commit()
    finally:
        conn.close()


def _make_data_root(root: Path, *, db_revision: str | None = "0001") -> None:
    """A minimal but complete production-data tree (flat layout) under *root*."""
    root.mkdir(parents=True, exist_ok=True)
    _make_db(root / "app.db", revision=db_revision)
    (root / "topology.xlsx").write_bytes(b"XLSX-CONTENT")

    extra = root / "compliance" / "extra"
    extra.mkdir(parents=True)
    (extra / "core2.tst-001.cfg").write_text("cfg-a")
    (extra / "rr1.tst-001.cfg").write_text("cfg-b")

    ignore = root / "compliance" / "ignore"
    ignore.mkdir(parents=True)
    (ignore / "Base.yaml").write_text("rules")
    (ignore / "notes.txt").write_text("")  # non-yaml must never travel

    sd = root / "services" / "definitions"
    sd.mkdir(parents=True)
    (sd / "bgp_def.yaml").write_text("asn: 65000")
    (sd / "bgp_lab_def.yaml").write_text("asn: lab")

    ad = root / "services" / "addressing"
    ad.mkdir(parents=True)
    (ad / "addressing_production_def.yaml").write_text("prod-addr")
    (ad / "addressing_lab_def.yaml").write_text("lab-addr")


_EXPECTED = {
    "app.db",
    "topology.xlsx",
    "compliance/extra/core2.tst-001.cfg",
    "compliance/extra/rr1.tst-001.cfg",
    "compliance/ignore/Base.yaml",
    "services/definitions/bgp_def.yaml",
    "services/definitions/bgp_lab_def.yaml",
    "services/addressing/addressing_production_def.yaml",
    "services/addressing/addressing_lab_def.yaml",
}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def test_build_contains_expected_files_and_manifest(tmp_path: Path) -> None:
    _make_data_root(tmp_path)
    zf = zipfile.ZipFile(io.BytesIO(build_bundle(data_root=tmp_path, environment="production")))
    members = {n for n in zf.namelist() if not n.endswith("/")}
    assert members == _EXPECTED | {"manifest.json"}
    # Every definition travels regardless of tenant — the server decides which
    # values you get, not a filename convention.
    assert "services/definitions/bgp_lab_def.yaml" in members
    assert "services/definitions/bgp_def.yaml" in members
    # Non-matching strays are still never carried.
    assert "compliance/ignore/notes.txt" not in members


def test_build_preserves_file_bytes(tmp_path: Path) -> None:
    _make_data_root(tmp_path)
    zf = zipfile.ZipFile(io.BytesIO(build_bundle(data_root=tmp_path, environment="production")))
    assert zf.read("services/definitions/bgp_def.yaml") == b"asn: 65000"
    assert zf.read("topology.xlsx") == b"XLSX-CONTENT"


def test_manifest_records_metadata_and_matching_checksums(tmp_path: Path) -> None:
    _make_data_root(tmp_path)
    data = build_bundle(data_root=tmp_path, environment="production", source_host="prod01")
    zf = zipfile.ZipFile(io.BytesIO(data))
    manifest = json.loads(zf.read("manifest.json"))

    assert manifest["format_version"] == 1
    assert manifest["source_host"] == "prod01"
    assert manifest["environment"] == "production"
    assert manifest["alembic_revision"] == "0001"
    assert {e["path"] for e in manifest["files"]} == _EXPECTED

    # Every manifest checksum matches the archived bytes.
    for entry in manifest["files"]:
        blob = zf.read(entry["path"])
        assert hashlib.sha256(blob).hexdigest() == entry["sha256"]
        assert entry["size"] == len(blob)


def test_build_omits_missing_singletons(tmp_path: Path) -> None:
    # A tree with only the compliance dir — no app.db, no workbook.
    extra = tmp_path / "compliance" / "extra"
    extra.mkdir(parents=True)
    (extra / "x.cfg").write_text("c")
    zf = zipfile.ZipFile(io.BytesIO(build_bundle(data_root=tmp_path, environment="test")))
    members = {n for n in zf.namelist() if not n.endswith("/")}
    assert members == {"compliance/extra/x.cfg", "manifest.json"}


def test_build_is_deterministic_apart_from_timestamp(tmp_path: Path) -> None:
    _make_data_root(tmp_path)
    a = build_bundle(data_root=tmp_path, environment="production", source_host="h")
    b = build_bundle(data_root=tmp_path, environment="production", source_host="h")
    za, zb = zipfile.ZipFile(io.BytesIO(a)), zipfile.ZipFile(io.BytesIO(b))
    for name in za.namelist():
        if name == core.MANIFEST_NAME:
            continue
        assert za.read(name) == zb.read(name)


# ---------------------------------------------------------------------------
# Schema introspection helper
# ---------------------------------------------------------------------------


def test_db_alembic_revision_reads_and_handles_absence(tmp_path: Path) -> None:
    with_rev = tmp_path / "with.db"
    without_rev = tmp_path / "without.db"
    _make_db(with_rev, revision="0007")
    _make_db(without_rev, revision=None)
    assert core._db_alembic_revision(with_rev) == "0007"
    assert core._db_alembic_revision(without_rev) is None
    assert core._db_alembic_revision(tmp_path / "missing.db") is None
