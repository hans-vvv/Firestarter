from __future__ import annotations

from pathlib import Path

from app.data_bundle.spec import (
    BUNDLE_DIRS,
    resolve_bundle_files,
    resolve_dir_files,
)


def _make_tree(root: Path) -> None:
    """A minimal data root (flat layout) with both production and lab files present."""
    (root / "app.db").write_bytes(b"db")
    (root / "topology.xlsx").write_bytes(b"xlsx")

    extra = root / "compliance" / "extra"
    extra.mkdir(parents=True)
    (extra / "core2.tst-001.cfg").write_text("cfg")
    (extra / "rr1.tst-001.cfg").write_text("cfg")
    (extra / "notes.txt").write_text("skip me")  # non-cfg → ignored

    ignore = root / "compliance" / "ignore"
    ignore.mkdir(parents=True)
    (ignore / "Base.yaml").write_text("rules")
    (ignore / "core2.tst-001.yaml").write_text("rules")
    (ignore / "notes.txt").write_text("skip me")  # non-yaml → ignored

    remediation = root / "compliance" / "remediation"
    remediation.mkdir(parents=True)
    (remediation / "pe.yaml").write_text("allow")
    (remediation / "agg.yaml").write_text("allow")
    (remediation / "README.txt").write_text("docs")  # non-yaml → ignored

    sd = root / "services" / "definitions"
    sd.mkdir(parents=True)
    (sd / "bgp_def.yaml").write_text("prod")
    (sd / "bgp_lab_def.yaml").write_text("lab")
    (sd / "notes.yaml").write_text("not a def")  # not *_def.yaml → ignored

    ad = root / "services" / "addressing"
    ad.mkdir(parents=True)
    (ad / "addressing_production_def.yaml").write_text("prod-addr")
    (ad / "addressing_lab_def.yaml").write_text("lab-addr")

    simulation = root / "simulation"
    simulation.mkdir(parents=True)
    (simulation / "drift.yaml").write_text("devices: {}")
    (simulation / "README.md").write_text("docs")  # non-yaml → ignored


def test_resolve_selects_all_environment_data(tmp_path: Path) -> None:
    """Every data file on the server is bundled — no lab/production filtering."""
    _make_tree(tmp_path)
    got = {p.as_posix() for p in resolve_bundle_files(data_root=tmp_path)}
    assert got == {
        "app.db",
        "topology.xlsx",
        "compliance/extra/core2.tst-001.cfg",
        "compliance/extra/rr1.tst-001.cfg",
        "compliance/ignore/Base.yaml",
        "compliance/ignore/core2.tst-001.yaml",
        "compliance/remediation/pe.yaml",
        "compliance/remediation/agg.yaml",
        "services/definitions/bgp_def.yaml",
        "services/definitions/bgp_lab_def.yaml",
        "services/addressing/addressing_production_def.yaml",
        "services/addressing/addressing_lab_def.yaml",
        "simulation/drift.yaml",
    }


def test_resolve_excludes_non_matching_files(tmp_path: Path) -> None:
    """Only the declared globs travel — strays and non-def yaml do not."""
    _make_tree(tmp_path)
    got = {p.as_posix() for p in resolve_bundle_files(data_root=tmp_path)}
    assert "compliance/ignore/notes.txt" not in got
    assert "compliance/extra/notes.txt" not in got
    assert "compliance/remediation/README.txt" not in got
    assert "services/definitions/notes.yaml" not in got
    assert "simulation/README.md" not in got


def test_resolve_skips_missing_singletons(tmp_path: Path) -> None:
    assert resolve_bundle_files(data_root=tmp_path) == []


def test_resolve_dir_missing_base_is_empty(tmp_path: Path) -> None:
    for bd in BUNDLE_DIRS:
        assert resolve_dir_files(bd, data_root=tmp_path) == []
