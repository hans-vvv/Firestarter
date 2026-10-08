from __future__ import annotations

"""Route + helper tests for the compliance-exceptions blueprint.

The two managed directories (extra/ and ignore/) are repointed at temp dirs so
the routes never touch the real exception files and writes/deletes can be
asserted on disk. Covers access control, path safety, create/edit/delete,
ignore-YAML validation gating, the protected base.yaml, and CRLF normalisation.
"""

from pathlib import Path

import pytest

import app.web.compliance_exceptions as ce

_VALID_IGNORE = 'ignore:\n  - match: startswith\n    value: "configure log"\n'
_INVALID_IGNORE = 'ignore:\n  - match: bogus\n    value: "x"\n'
_EXTRA_CFG = "configure router policy-options foo\n"


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """Temp extra/ + ignore/ dirs wired into the helper via monkeypatch."""
    extra = tmp_path / "extra"
    ignore = tmp_path / "ignore"
    extra.mkdir()
    ignore.mkdir()
    (extra / "pe1.tst-001.cfg").write_text(_EXTRA_CFG, encoding="utf-8")
    (ignore / "base.yaml").write_text(_VALID_IGNORE, encoding="utf-8")
    (ignore / "pe1.tst-001.yaml").write_text(_VALID_IGNORE, encoding="utf-8")
    monkeypatch.setattr(ce, "EXTRA_DIR", extra)
    monkeypatch.setattr(ce, "IGNORE_DIR", ignore)
    return {"extra": extra, "ignore": ignore}


# ── pure-helper tests (the safety logic) ──────────────────────────────────────


class TestHelpers:
    def test_safe_path_rejects_separators_and_traversal(self, dirs):
        assert ce.safe_path("ignore", "../base.yaml") is None
        assert ce.safe_path("ignore", "sub/base.yaml") is None
        assert ce.safe_path("ignore", "..") is None
        assert ce.safe_path("ignore", "back\\slash.yaml") is None

    def test_safe_path_requires_section_extension(self, dirs):
        assert ce.safe_path("extra", "host.yaml") is None
        assert ce.safe_path("ignore", "host.cfg") is None
        assert ce.safe_path("extra", "host.cfg") is not None
        assert ce.safe_path("ignore", "host.yaml") is not None

    def test_safe_path_unknown_section(self, dirs):
        assert ce.safe_path("nope", "host.cfg") is None

    def test_list_files_filters_by_extension(self, dirs):
        (dirs["ignore"] / "__init__.py").write_text("", encoding="utf-8")
        files = ce.list_files("ignore")
        assert "base.yaml" in files
        assert "__init__.py" not in files

    def test_normalise_appends_extension(self, dirs):
        assert ce.normalise_new_filename("ignore", "pe2.tst-001") == (
            "pe2.tst-001.yaml",
            None,
        )
        assert ce.normalise_new_filename("extra", "role") == ("role.cfg", None)

    def test_normalise_rejects_empty_and_bad_chars(self, dirs):
        name, err = ce.normalise_new_filename("extra", "   ")
        assert name is None
        assert err
        name, err = ce.normalise_new_filename("extra", "../evil")
        assert name is None
        assert err

    def test_is_protected(self, dirs):
        assert ce.is_protected("ignore", "base.yaml")
        assert ce.is_protected("ignore", "BASE.YAML")  # case-insensitive
        assert not ce.is_protected("ignore", "pe1.tst-001.yaml")
        assert not ce.is_protected("extra", "anything.cfg")

    def test_validate_content_extra_accepts_anything(self, dirs):
        assert ce.validate_content("extra", "literally\nany text") is None

    def test_validate_content_ignore_valid(self, dirs):
        assert ce.validate_content("ignore", _VALID_IGNORE) is None

    def test_validate_content_ignore_bad_rule(self, dirs):
        err = ce.validate_content("ignore", _INVALID_IGNORE)
        assert err is not None
        assert "bogus" in err

    def test_validate_content_ignore_bad_yaml(self, dirs):
        err = ce.validate_content("ignore", "ignore: : [")
        assert err is not None
        assert "YAML syntax error" in err

    def test_validate_content_ignore_empty_ok(self, dirs):
        assert ce.validate_content("ignore", "") is None


# ── route tests ───────────────────────────────────────────────────────────────


class TestAccess:
    def test_anonymous_redirected(self, anon_client):
        resp = anon_client.get("/compliance-exceptions")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_index_renders(self, client, dirs):
        resp = client.get("/compliance-exceptions")
        assert resp.status_code == 200
        assert b"Compliance exceptions" in resp.data
        assert b"pe1.tst-001.cfg" in resp.data
        assert b"base.yaml" in resp.data

    def test_ro_sees_readonly_view(self, login_as, dirs):
        resp = login_as(role="ro").get("/compliance-exceptions/extra/pe1.tst-001.cfg/edit")
        assert resp.status_code == 200
        assert b"<textarea" not in resp.data
        assert b"<pre" in resp.data

    def test_rw_sees_editor(self, login_as, dirs):
        resp = login_as(role="rw").get("/compliance-exceptions/extra/pe1.tst-001.cfg/edit")
        assert resp.status_code == 200
        assert b"<textarea" in resp.data

    def test_ro_cannot_save(self, login_as, dirs):
        resp = login_as(role="ro").post(
            "/compliance-exceptions/extra/pe1.tst-001.cfg/edit",
            data={"content": "x"},
        )
        assert resp.status_code == 403

    def test_ro_cannot_create(self, login_as, dirs):
        resp = login_as(role="ro").post(
            "/compliance-exceptions/extra/create", data={"filename": "new"}
        )
        assert resp.status_code == 403

    def test_ro_cannot_delete(self, login_as, dirs):
        resp = login_as(role="ro").post("/compliance-exceptions/extra/pe1.tst-001.cfg/delete")
        assert resp.status_code == 403


class TestEditSave:
    def test_valid_extra_save_writes_file(self, login_as, dirs):
        resp = login_as(role="rw").post(
            "/compliance-exceptions/extra/pe1.tst-001.cfg/edit",
            data={"content": "configure new line"},
        )
        assert resp.status_code == 200
        assert b"Saved successfully" in resp.data
        assert "configure new line" in (dirs["extra"] / "pe1.tst-001.cfg").read_text(
            encoding="utf-8"
        )

    def test_valid_ignore_save_writes_file(self, login_as, dirs):
        new = _VALID_IGNORE.replace("configure log", "configure system")
        resp = login_as(role="rw").post(
            "/compliance-exceptions/ignore/pe1.tst-001.yaml/edit",
            data={"content": new},
        )
        assert resp.status_code == 200
        assert b"Saved successfully" in resp.data
        assert "configure system" in (dirs["ignore"] / "pe1.tst-001.yaml").read_text(
            encoding="utf-8"
        )

    def test_invalid_ignore_rejected_and_file_untouched(self, login_as, dirs):
        before = (dirs["ignore"] / "pe1.tst-001.yaml").read_text(encoding="utf-8")
        resp = login_as(role="rw").post(
            "/compliance-exceptions/ignore/pe1.tst-001.yaml/edit",
            data={"content": _INVALID_IGNORE},
        )
        assert resp.status_code == 200
        assert b"bogus" in resp.data
        assert (dirs["ignore"] / "pe1.tst-001.yaml").read_text(encoding="utf-8") == before

    def test_crlf_normalised_to_lf(self, login_as, dirs):
        crlf = _VALID_IGNORE.replace("\n", "\r\n")
        resp = login_as(role="rw").post(
            "/compliance-exceptions/ignore/pe1.tst-001.yaml/edit",
            data={"content": crlf},
        )
        assert resp.status_code == 200
        assert b"\r\n" not in (dirs["ignore"] / "pe1.tst-001.yaml").read_bytes()

    def test_edit_unknown_file_404(self, login_as, dirs):
        resp = login_as(role="rw").get("/compliance-exceptions/extra/nope.cfg/edit")
        assert resp.status_code == 404

    def test_save_traversal_filename_404(self, login_as, dirs):
        # A bad-extension filename is rejected by safe_path → 404, never written.
        resp = login_as(role="rw").post(
            "/compliance-exceptions/extra/evil.txt/edit", data={"content": "x"}
        )
        assert resp.status_code == 404


class TestCreate:
    def test_create_extra_file(self, login_as, dirs):
        resp = login_as(role="rw").post(
            "/compliance-exceptions/extra/create", data={"filename": "core9.tst-001"}
        )
        assert resp.status_code == 200
        assert (dirs["extra"] / "core9.tst-001.cfg").exists()
        assert (dirs["extra"] / "core9.tst-001.cfg").read_text(encoding="utf-8") == ""

    def test_create_duplicate_rejected(self, login_as, dirs):
        resp = login_as(role="rw").post(
            "/compliance-exceptions/extra/create",
            data={"filename": "pe1.tst-001.cfg"},
        )
        assert resp.status_code == 200
        assert b"already exists" in resp.data

    def test_create_bad_filename_rejected(self, login_as, dirs):
        resp = login_as(role="rw").post(
            "/compliance-exceptions/extra/create", data={"filename": "../evil"}
        )
        assert resp.status_code == 200
        assert b"Invalid filename" in resp.data
        assert not (dirs["extra"] / "evil.cfg").exists()


class TestDelete:
    def test_delete_file(self, login_as, dirs):
        assert (dirs["extra"] / "pe1.tst-001.cfg").exists()
        resp = login_as(role="rw").post("/compliance-exceptions/extra/pe1.tst-001.cfg/delete")
        assert resp.status_code == 200
        assert not (dirs["extra"] / "pe1.tst-001.cfg").exists()

    def test_cannot_delete_protected_base(self, login_as, dirs):
        resp = login_as(role="rw").post("/compliance-exceptions/ignore/base.yaml/delete")
        assert resp.status_code == 200
        assert b"cannot be deleted" in resp.data
        assert (dirs["ignore"] / "base.yaml").exists()

    def test_delete_unknown_file_404(self, login_as, dirs):
        resp = login_as(role="rw").post("/compliance-exceptions/extra/nope.cfg/delete")
        assert resp.status_code == 404
