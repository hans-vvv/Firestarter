from __future__ import annotations

from pathlib import Path

from app.data_bundle.seed_worktree import seed_worktree


def _make_source(root: Path) -> None:
    """A minimal source data root in the flat layout."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.db").write_bytes(b"DBDATA")
    (root / "topology.xlsx").write_bytes(b"XLSX")
    sd = root / "services" / "definitions"
    sd.mkdir(parents=True)
    (sd / "bgp_def.yaml").write_text("prod")
    (sd / "bgp_lab_def.yaml").write_text("lab")


def test_seed_copies_bundle_files_into_empty_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source-data"
    dest = tmp_path / "worktree-data"
    _make_source(source)
    dest.mkdir()

    copied = seed_worktree(source_root=source, dest_root=dest)

    assert "app.db" in copied
    assert "topology.xlsx" in copied
    # Every definition is seeded — a fresh worktree has no YAML at all until this
    # runs, so both tenants' files must arrive.
    assert "services/definitions/bgp_def.yaml" in copied
    assert "services/definitions/bgp_lab_def.yaml" in copied

    assert (dest / "app.db").read_bytes() == b"DBDATA"
    assert (dest / "services/definitions/bgp_def.yaml").read_text() == "prod"
    assert (dest / "services/definitions/bgp_lab_def.yaml").read_text() == "lab"


def test_seed_overwrites_existing_with_source_content(tmp_path: Path) -> None:
    source = tmp_path / "source-data"
    dest = tmp_path / "worktree-data"
    _make_source(source)
    # dest already has a stale app.db (as a prior seed might).
    dest.mkdir()
    (dest / "app.db").write_bytes(b"STALE")

    seed_worktree(source_root=source, dest_root=dest)

    assert (dest / "app.db").read_bytes() == b"DBDATA"
