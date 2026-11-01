from __future__ import annotations

"""Route tests for the service-definition editor blueprint.

The catalogue functions are repointed at a temp directory of fixture files so
the routes never touch the real definitions, and so writes can be asserted on
disk. Covers access control, the htmx partial responses, validation gating,
atomic save, and CRLF normalisation.
"""

from pathlib import Path

import pytest

import app.web.routes.services as services
from app.web.service_catalogue import build_catalogue, resolve_slug

_VALID_BGP = """\
service: bgp
tenant: lab
variant: default

selectors:
  devices:
    bgp:
      match:
        labels:
          tenant: lab

features:
  bgp:
    asn: 65000

interface_features: {}

parameters: {}
"""


@pytest.fixture
def defs_dir(tmp_path: Path, monkeypatch) -> Path:
    """Temp definitions dir wired into the routes via monkeypatched facades."""
    (tmp_path / "bgp_lab_def.yaml").write_text(_VALID_BGP, encoding="utf-8")
    monkeypatch.setattr(services, "build_catalogue", lambda: build_catalogue(defs_dir=tmp_path))
    monkeypatch.setattr(
        services, "resolve_slug", lambda slug: resolve_slug(slug, defs_dir=tmp_path)
    )
    return tmp_path


_SLUG = "infra/lab/bgp"
_EDIT_URL = f"/services/{_SLUG}/edit"


class TestAccess:
    def test_anonymous_redirected(self, anon_client):
        resp = anon_client.get("/services")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_index_renders(self, client, defs_dir):
        resp = client.get("/services")
        assert resp.status_code == 200
        assert b"Infrastructure" in resp.data

    def test_ro_sees_readonly_view(self, login_as, defs_dir):
        resp = login_as(role="ro").get(_EDIT_URL)
        assert resp.status_code == 200
        assert b"<textarea" not in resp.data
        assert b"<pre" in resp.data

    def test_rw_sees_editor(self, login_as, defs_dir):
        resp = login_as(role="rw").get(_EDIT_URL)
        assert resp.status_code == 200
        assert b"<textarea" in resp.data

    def test_ro_cannot_save(self, login_as, defs_dir):
        resp = login_as(role="ro").post(_EDIT_URL, data={"content": _VALID_BGP})
        assert resp.status_code == 403


class TestSave:
    def test_valid_save_writes_file(self, login_as, defs_dir):
        new = _VALID_BGP.replace("asn: 65000", "asn: 65111")
        resp = login_as(role="rw").post(_EDIT_URL, data={"content": new})
        assert resp.status_code == 200
        assert b"Saved successfully" in resp.data
        assert "asn: 65111" in (defs_dir / "bgp_lab_def.yaml").read_text(encoding="utf-8")

    def test_invalid_yaml_rejected_and_file_untouched(self, login_as, defs_dir):
        before = (defs_dir / "bgp_lab_def.yaml").read_text(encoding="utf-8")
        resp = login_as(role="rw").post(_EDIT_URL, data={"content": "service: : ["})
        assert resp.status_code == 200
        assert b"YAML syntax error" in resp.data
        assert (defs_dir / "bgp_lab_def.yaml").read_text(encoding="utf-8") == before

    def test_schema_invalid_rejected_and_file_untouched(self, login_as, defs_dir):
        before = (defs_dir / "bgp_lab_def.yaml").read_text(encoding="utf-8")
        resp = login_as(role="rw").post(_EDIT_URL, data={"content": "service: bgp\n"})
        assert resp.status_code == 200
        assert b"Validation error" in resp.data
        assert (defs_dir / "bgp_lab_def.yaml").read_text(encoding="utf-8") == before

    def test_crlf_normalised_to_lf(self, login_as, defs_dir):
        crlf = _VALID_BGP.replace("\n", "\r\n")
        resp = login_as(role="rw").post(_EDIT_URL, data={"content": crlf})
        assert resp.status_code == 200
        written = (defs_dir / "bgp_lab_def.yaml").read_bytes()
        assert b"\r\n" not in written


class TestNotFound:
    def test_editor_unknown_slug(self, login_as, defs_dir):
        resp = login_as(role="rw").get("/services/services/nowhere/vpls/x/edit")
        assert resp.status_code == 404
        assert b"No service definition found" in resp.data

    def test_save_unknown_slug(self, login_as, defs_dir):
        resp = login_as(role="rw").post(
            "/services/services/nowhere/vpls/x/edit", data={"content": _VALID_BGP}
        )
        assert resp.status_code == 404
