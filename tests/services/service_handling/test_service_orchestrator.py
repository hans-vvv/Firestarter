from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

import app.services.service_handling.service_orchestrator as orch_mod
from app.domain.file_locations import FileLocations
from app.services.service_handling.service_orchestrator import ServiceOrchestrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_def(directory: Path, name: str, service: str, tenant: str, variant: str) -> Path:
    """Write a minimal schema-compliant service definition YAML file."""
    path = directory / name
    path.write_text(
        yaml.dump(
            {
                "service": service,
                "tenant": tenant,
                "variant": variant,
                "selectors": {"devices": {}},
                "features": {},
                "interface_features": {},
                "parameters": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def _make_orchestrator() -> ServiceOrchestrator:
    return ServiceOrchestrator(service_builder=MagicMock())


# ---------------------------------------------------------------------------
# _discover_definitions_by_key
# ---------------------------------------------------------------------------


class TestDiscoverDefinitionsByKey:
    def test_discovers_valid_file(self, tmp_path, monkeypatch):
        _write_def(tmp_path, "isis_lab_def.yaml", "isis", "lab", "default")
        monkeypatch.setattr(orch_mod, "SERVICES_DEF_LOC", FileLocations(location=str(tmp_path)))

        so = _make_orchestrator()
        result = so._discover_definitions_by_key()

        assert ("isis", "lab", "default") in result

    def test_ignores_files_not_ending_in_def_yaml(self, tmp_path, monkeypatch):
        (tmp_path / "notes.yaml").write_text("service: isis\ntenant: lab\nvariant: x\n")
        monkeypatch.setattr(orch_mod, "SERVICES_DEF_LOC", FileLocations(location=str(tmp_path)))

        so = _make_orchestrator()
        result = so._discover_definitions_by_key()
        assert result == {}

    def test_empty_directory_returns_empty_dict(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orch_mod, "SERVICES_DEF_LOC", FileLocations(location=str(tmp_path)))

        so = _make_orchestrator()
        assert so._discover_definitions_by_key() == {}

    def test_duplicate_key_raises(self, tmp_path, monkeypatch):
        _write_def(tmp_path, "isis_lab_def.yaml", "isis", "lab", "default")
        _write_def(tmp_path, "isis_lab2_def.yaml", "isis", "lab", "default")
        monkeypatch.setattr(orch_mod, "SERVICES_DEF_LOC", FileLocations(location=str(tmp_path)))

        so = _make_orchestrator()
        with pytest.raises(ValueError, match="Duplicate"):
            so._discover_definitions_by_key()

    def test_multiple_files_indexed_correctly(self, tmp_path, monkeypatch):
        _write_def(tmp_path, "isis_lab_def.yaml", "isis", "lab", "default")
        _write_def(tmp_path, "bgp_lab_def.yaml", "bgp", "lab", "default")
        monkeypatch.setattr(orch_mod, "SERVICES_DEF_LOC", FileLocations(location=str(tmp_path)))

        so = _make_orchestrator()
        result = so._discover_definitions_by_key()
        assert ("isis", "lab", "default") in result
        assert ("bgp", "lab", "default") in result

    def test_missing_service_field_raises(self, tmp_path, monkeypatch):
        path = tmp_path / "bad_def.yaml"
        path.write_text(yaml.dump({"tenant": "lab", "variant": "default"}))
        monkeypatch.setattr(orch_mod, "SERVICES_DEF_LOC", FileLocations(location=str(tmp_path)))

        so = _make_orchestrator()
        with pytest.raises(Exception, match="service"):
            so._discover_definitions_by_key()

    def test_missing_tenant_field_raises(self, tmp_path, monkeypatch):
        path = tmp_path / "bad_def.yaml"
        path.write_text(yaml.dump({"service": "isis", "variant": "default"}))
        monkeypatch.setattr(orch_mod, "SERVICES_DEF_LOC", FileLocations(location=str(tmp_path)))

        so = _make_orchestrator()
        with pytest.raises(Exception, match="tenant"):
            so._discover_definitions_by_key()


# ---------------------------------------------------------------------------
# submit — ordering guarantee
# ---------------------------------------------------------------------------


class TestSubmitOrdering:
    def test_submit_calls_compute_for_each_definition(self, tmp_path, monkeypatch):
        _write_def(tmp_path, "isis_lab_def.yaml", "isis", "lab", "default")
        monkeypatch.setattr(orch_mod, "SERVICES_DEF_LOC", FileLocations(location=str(tmp_path)))

        sb = MagicMock()
        so = ServiceOrchestrator(service_builder=sb)
        so.submit()

        assert sb.compute.call_count == 1

    def test_submit_with_no_definitions_does_not_call_compute(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orch_mod, "SERVICES_DEF_LOC", FileLocations(location=str(tmp_path)))

        sb = MagicMock()
        so = ServiceOrchestrator(service_builder=sb)
        so.submit()

        sb.compute.assert_not_called()


# ---------------------------------------------------------------------------
# submit — orphan pruning
# ---------------------------------------------------------------------------


class TestSubmitPrunesOrphans:
    def test_prune_called_with_current_def_keys(self, tmp_path, monkeypatch):
        _write_def(tmp_path, "isis_lab_def.yaml", "isis", "lab", "default")
        _write_def(tmp_path, "bgp_lab_def.yaml", "bgp", "lab", "default")
        monkeypatch.setattr(orch_mod, "SERVICES_DEF_LOC", FileLocations(location=str(tmp_path)))

        sb = MagicMock()
        sb.prune_orphans.return_value = []
        so = ServiceOrchestrator(service_builder=sb)
        so.submit()

        sb.prune_orphans.assert_called_once_with(
            valid_keys={("isis", "lab", "default"), ("bgp", "lab", "default")}
        )

    def test_prune_called_even_with_no_definitions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orch_mod, "SERVICES_DEF_LOC", FileLocations(location=str(tmp_path)))

        sb = MagicMock()
        sb.prune_orphans.return_value = []
        so = ServiceOrchestrator(service_builder=sb)
        so.submit()

        # An empty definition set means every existing instance is an orphan.
        sb.prune_orphans.assert_called_once_with(valid_keys=set())

    def test_submit_processes_tenants_in_sorted_order(self, tmp_path, monkeypatch):
        _write_def(tmp_path, "isis_zzz_def.yaml", "isis", "zzz", "default")
        _write_def(tmp_path, "isis_aaa_def.yaml", "isis", "aaa", "default")
        monkeypatch.setattr(orch_mod, "SERVICES_DEF_LOC", FileLocations(location=str(tmp_path)))

        sb = MagicMock()
        so = ServiceOrchestrator(service_builder=sb)
        so.submit()

        calls = sb.compute.call_args_list
        assert len(calls) == 2
        tenants = [c.args[0]["service_data"]["tenant"] for c in calls]
        assert tenants == sorted(tenants)

    def test_submit_processes_variants_in_sorted_order(self, tmp_path, monkeypatch):
        _write_def(tmp_path, "isis_lab_zzz_def.yaml", "isis", "lab", "zzz")
        _write_def(tmp_path, "isis_lab_aaa_def.yaml", "isis", "lab", "aaa")
        monkeypatch.setattr(orch_mod, "SERVICES_DEF_LOC", FileLocations(location=str(tmp_path)))

        sb = MagicMock()
        so = ServiceOrchestrator(service_builder=sb)
        so.submit()

        calls = sb.compute.call_args_list
        variants = [c.args[0]["service_data"]["variant"] for c in calls]
        assert variants == sorted(variants)


# ---------------------------------------------------------------------------
# _load_service_yaml
# ---------------------------------------------------------------------------


class TestLoadServiceYaml:
    def test_loads_yaml_file(self, tmp_path):
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump({"key": "value"}))
        result = _make_orchestrator()._load_service_yaml(path)
        assert result == {"key": "value"}

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ValueError, match="YAML file not found"):
            _make_orchestrator()._load_service_yaml(tmp_path / "nonexistent.yaml")

    def test_memoises_within_one_orchestrator(self, tmp_path):
        """The reason the cache exists: each definition is parsed more than once
        per run — submit() discovers them and _validate_yaml_service_files()
        discovers them again."""
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump({"key": "first"}))
        so = _make_orchestrator()

        first = so._load_service_yaml(path)
        path.write_text(yaml.dump({"key": "second"}))

        assert so._load_service_yaml(path) is first

    def test_a_new_orchestrator_re_reads_the_file(self, tmp_path):
        """The bug this fixes. Under gunicorn the worker lives for days, so a
        process-wide cache meant a definition edited in the dashboard was never
        re-read — the pipeline silently computed from the stale copy."""
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump({"key": "first"}))
        assert _make_orchestrator()._load_service_yaml(path) == {"key": "first"}

        path.write_text(yaml.dump({"key": "second"}))

        assert _make_orchestrator()._load_service_yaml(path) == {"key": "second"}

    def test_two_orchestrators_do_not_share_a_cache(self, tmp_path):
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump({"key": "value"}))
        a, b = _make_orchestrator(), _make_orchestrator()

        a._load_service_yaml(path)

        assert b._yaml_cache == {}

    def test_the_same_path_spelled_differently_hits_one_entry(self, tmp_path):
        """Paths are resolved before caching, so ./x and x are one key."""
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump({"key": "value"}))
        so = _make_orchestrator()

        so._load_service_yaml(path)
        so._load_service_yaml(tmp_path / "." / "test.yaml")

        assert len(so._yaml_cache) == 1
