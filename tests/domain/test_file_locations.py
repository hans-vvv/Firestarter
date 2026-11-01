"""The single data-root registry: default layout, env override, back-compat.

These tests pin the Sprint-A contract that every per-environment data path and
writable output directory resolves under one ``data_root()``, overridable by the
``FIRESTARTER_DATA`` environment variable — the seam a container uses to mount
all mutable state on one volume while the image stays read-only code.
"""

from __future__ import annotations

from pathlib import Path

from app.config import get_database_url
from app.domain.file_locations import (
    ADDRESSING_DEF_LOC,
    ARTIFACTS_LOC,
    BACKUPS_LOC,
    COMPLIANCE_EXTRA_LOC,
    COMPLIANCE_IGNORE_LOC,
    COMPLIANCE_REMEDIATION_LOC,
    COMPLIANCE_REPORTS_LOC,
    GENERATED_LOC,
    LOGS_LOC,
    PRODUCTION_DB_LOC,
    SERVICES_DEF_LOC,
    SIMULATION_LOC,
    TOPOLOGY_EXCEL_LOC,
    FileLocations,
    data_root,
)

# Every declared location, so a newly added one is covered automatically.
ALL_LOCS = [
    ADDRESSING_DEF_LOC,
    SERVICES_DEF_LOC,
    TOPOLOGY_EXCEL_LOC,
    PRODUCTION_DB_LOC,
    COMPLIANCE_IGNORE_LOC,
    COMPLIANCE_EXTRA_LOC,
    COMPLIANCE_REMEDIATION_LOC,
    COMPLIANCE_REPORTS_LOC,
    BACKUPS_LOC,
    ARTIFACTS_LOC,
    GENERATED_LOC,
    LOGS_LOC,
    SIMULATION_LOC,
]


def _repo_root() -> Path:
    # tests/domain/test_file_locations.py → parents[2] is the repo root.
    return Path(__file__).resolve().parents[2]


def test_data_root_defaults_to_repo_data_dir(monkeypatch):
    """With no env override, the data root is ``<repo>/data`` — a single gitignored
    directory holding no code (Sprint B)."""
    monkeypatch.delenv("FIRESTARTER_DATA", raising=False)
    assert data_root() == _repo_root() / "data"


def test_data_root_follows_env_override(monkeypatch, tmp_path):
    """``FIRESTARTER_DATA`` repoints the data root live (read per call, not frozen)."""
    monkeypatch.setenv("FIRESTARTER_DATA", str(tmp_path))
    assert data_root() == tmp_path


def test_every_location_resolves_under_the_live_data_root(monkeypatch, tmp_path):
    """Every declared path lands under the override, preserving its relative location."""
    monkeypatch.setenv("FIRESTARTER_DATA", str(tmp_path))
    for loc in ALL_LOCS:
        assert loc.path == tmp_path / loc.location


def test_location_string_is_unchanged_by_override(monkeypatch, tmp_path):
    """``.location`` is the data-root-relative string the bundle/seed taxonomy uses;
    the env override must not mutate it — only where it resolves to."""
    before = {loc.location for loc in ALL_LOCS}
    monkeypatch.setenv("FIRESTARTER_DATA", str(tmp_path))
    after = {loc.location for loc in ALL_LOCS}
    assert before == after


def test_absolute_location_is_returned_unchanged(monkeypatch, tmp_path):
    """A FileLocations built from an absolute path (as test doubles do) resolves to
    exactly that path, regardless of the data root."""
    monkeypatch.setenv("FIRESTARTER_DATA", str(tmp_path / "ignored"))
    absolute = tmp_path / "some" / "dir"
    assert FileLocations(location=str(absolute)).path == absolute


def test_default_database_url_tracks_the_data_root(monkeypatch, tmp_path):
    """The default SQLite URL (no DATABASE_URL) points at the DB under the data root."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FIRESTARTER_DATA", str(tmp_path))
    assert get_database_url() == f"sqlite:///{(tmp_path / PRODUCTION_DB_LOC.location).as_posix()}"


def test_explicit_database_url_wins_over_data_root(monkeypatch, tmp_path):
    """An explicit DATABASE_URL overrides the data-root default entirely."""
    monkeypatch.setenv("FIRESTARTER_DATA", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///tmp/explicit.db")
    assert get_database_url() == "sqlite:///tmp/explicit.db"


def test_consumer_module_constants_follow_the_registry(monkeypatch):
    """Module-level seams (resolved at import) point under the live data root.

    The expected root is derived from ``data_root()`` rather than hardcoded to
    ``<repo>/data``, so this passes whether ``FIRESTARTER_DATA`` is unset (a plain
    checkout) or set (a container with a mounted volume) — ``FIRESTARTER_DATA`` is
    constant within a process, so the import-time constants and ``data_root()`` agree.
    """
    from app.automation import backup, inventory, simulated_devices
    from app.compliance import normaliser
    from app.logging import logger
    from app.web import service_catalogue

    root = data_root()
    assert root / COMPLIANCE_IGNORE_LOC.location == normaliser.IGNORE_DIR
    assert root / COMPLIANCE_EXTRA_LOC.location == normaliser.EXTRA_DIR
    assert root / BACKUPS_LOC.location == backup.BACKUPS_DIR
    assert root / SIMULATION_LOC.location / "drift.yaml" == simulated_devices.DRIFT_FILE
    # Fixes from the "finish the split" sprint:
    assert root / SERVICES_DEF_LOC.location == service_catalogue._DEFS_DIR
    assert root / GENERATED_LOC.location == inventory.DEFAULT_OUT_DIR
    # Logs default under the data root when FIRESTARTER_LOG_DIR is not set (the
    # suite otherwise points it at a tmp dir via conftest).
    monkeypatch.delenv("FIRESTARTER_LOG_DIR", raising=False)
    assert root / LOGS_LOC.location == logger._log_dir()
