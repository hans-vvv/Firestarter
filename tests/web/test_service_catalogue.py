from __future__ import annotations

"""Unit tests for the dynamic service-definition catalogue.

These exercise the classification logic that maps parsed YAML header fields
(service / tenant / variant) onto categories and URL slugs, plus the on-disk
discovery behaviour — including the key requirement that a server holding only
lab files shows only lab entries.
"""

from pathlib import Path

import pytest

from app.web.service_catalogue import build_catalogue, resolve_slug


def _write(dir_: Path, filename: str, *, service: str, tenant: str, variant: str) -> None:
    (dir_ / filename).write_text(
        f"service: {service}\ntenant: {tenant}\nvariant: {variant}\n",
        encoding="utf-8",
    )


# Representative slice of a definition set: both categories, both tenants, and
# several variants of the customer-facing services.
_SAMPLE = [
    ("isis_def.yaml", "isis", "production", "default"),
    ("isis_lab_def.yaml", "isis", "lab", "default"),
    ("bgp_def.yaml", "bgp", "production", "default"),
    ("evpn_esi_def.yaml", "evpn_esi", "production", "default"),
    ("sr_pe_def.yaml", "sr", "production", "pe"),
    ("sr_core_def.yaml", "sr", "production", "core"),
    ("evpn_vpls_ce_mgmt_def.yaml", "evpn_vpls", "production", "ce_mgmt"),
    ("evpn_vpls_customer_def.yaml", "evpn_vpls", "production", "customer_l2"),
    ("vprn_ce_mgmt_def.yaml", "vprn", "production", "ce_mgmt"),
    ("vprn_customer_lab_def.yaml", "vprn", "lab", "customer_l3"),
]


@pytest.fixture
def defs_dir(tmp_path: Path) -> Path:
    for filename, service, tenant, variant in _SAMPLE:
        _write(tmp_path, filename, service=service, tenant=tenant, variant=variant)
    return tmp_path


class TestClassification:
    def test_infra_slugs(self, defs_dir):
        slugs = {e.url_slug for e in build_catalogue(defs_dir=defs_dir).infra}
        assert slugs == {
            "infra/production/isis",
            "infra/lab/isis",
            "infra/production/bgp",
            "infra/production/evpn-esi",
            "infra/production/sr-pe",
            "infra/production/sr-core",
        }

    def test_service_slugs_carry_tenant_type_and_variant(self, defs_dir):
        slugs = {e.url_slug for e in build_catalogue(defs_dir=defs_dir).services}
        assert slugs == {
            "services/production/vpls/ce-mgmt",
            "services/production/vpls/customer-l2",
            "services/production/vprn/ce-mgmt",
            "services/lab/vprn/customer-l3",
        }

    def test_badge_is_the_tenant(self, defs_dir):
        cat = build_catalogue(defs_dir=defs_dir)
        assert {e.env_badge for e in cat.all_entries()} == {"production", "lab"}

    def test_labels_name_service_variant_and_tenant(self, defs_dir):
        labels = {e.label for e in build_catalogue(defs_dir=defs_dir).services}
        assert "VPRN ce_mgmt · production" in labels
        assert "VPLS customer_l2 · production" in labels


class TestDiscovery:
    def test_lab_only_directory_shows_only_lab(self, tmp_path):
        # Mirrors the future split where a lab server holds only lab files.
        _write(tmp_path, "isis_lab_def.yaml", service="isis", tenant="lab", variant="default")
        _write(tmp_path, "bgp_lab_def.yaml", service="bgp", tenant="lab", variant="default")
        cat = build_catalogue(defs_dir=tmp_path)
        slugs = {e.url_slug for e in cat.infra}
        assert slugs == {"infra/lab/isis", "infra/lab/bgp"}
        assert all("production" not in s for s in slugs)

    def test_empty_directory(self, tmp_path):
        assert build_catalogue(defs_dir=tmp_path).is_empty()

    def test_non_def_files_ignored(self, tmp_path):
        # Only *_def.yaml / *_def.yml are scanned.
        (tmp_path / "notes.yaml").write_text("service: isis\ntenant: lab\nvariant: x\n")
        _write(tmp_path, "isis_lab_def.yaml", service="isis", tenant="lab", variant="default")
        cat = build_catalogue(defs_dir=tmp_path)
        assert len(cat.infra) == 1

    def test_malformed_yaml_skipped(self, tmp_path):
        (tmp_path / "broken_def.yaml").write_text("service: : [\n", encoding="utf-8")
        _write(tmp_path, "isis_lab_def.yaml", service="isis", tenant="lab", variant="default")
        cat = build_catalogue(defs_dir=tmp_path)  # must not raise
        assert len(cat.infra) == 1

    def test_unknown_service_skipped(self, tmp_path):
        _write(tmp_path, "mystery_def.yaml", service="wormhole", tenant="lab", variant="x")
        assert build_catalogue(defs_dir=tmp_path).is_empty()

    def test_missing_field_skipped(self, tmp_path):
        (tmp_path / "partial_def.yaml").write_text("service: isis\ntenant: lab\n", encoding="utf-8")
        assert build_catalogue(defs_dir=tmp_path).is_empty()

    def test_duplicate_slug_deduplicated(self, tmp_path):
        _write(tmp_path, "isis_lab_def.yaml", service="isis", tenant="lab", variant="default")
        _write(tmp_path, "isis_lab_copy_def.yaml", service="isis", tenant="lab", variant="default")
        cat = build_catalogue(defs_dir=tmp_path)
        assert len(cat.infra) == 1


class TestResolveSlug:
    def test_round_trip(self, defs_dir):
        cat = build_catalogue(defs_dir=defs_dir)
        assert cat.all_entries()
        for entry in cat.all_entries():
            resolved = resolve_slug(entry.url_slug, defs_dir=defs_dir)
            assert resolved is not None
            assert resolved.name == entry.filename

    def test_unknown_slug_returns_none(self, defs_dir):
        assert resolve_slug("services/nowhere/vpls/x", defs_dir=defs_dir) is None

    def test_traversal_attempt_returns_none(self, defs_dir):
        # A slug is only ever matched against catalogue entries, never joined to
        # the filesystem path, so traversal sequences simply fail to resolve.
        assert resolve_slug("../../../etc/passwd", defs_dir=defs_dir) is None
