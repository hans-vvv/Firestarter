from __future__ import annotations

"""Route + helper tests for the compliance-remediations blueprint.

The remediation directory is repointed at a temp dir so the routes never touch
the real per-role spec files and writes/deletes can be asserted on disk. Covers
access control, path safety, create/edit/delete, spec-YAML validation gating, and
CRLF normalisation.
"""

from pathlib import Path

import pytest

import app.web.remediations as rem

_VALID_SPEC = (
    "add:\n"
    "  - match: startswith\n"
    '    value: "configure router bgp"\n'
    "delete:\n"
    "  - match: startswith\n"
    '    value: "configure router \\"Base\\" bgp neighbor \\"192.0.2.17\\""\n'
    "    remediation_commands:\n"
    '      - delete router "Base" bgp neighbor "192.0.2.17"\n'
    "conditions:\n"
    "  status: active\n"
    "  labels:\n"
    '    rr_migrated: "True"\n'
)
_BAD_MATCH = 'add:\n  - match: bogus\n    value: "x"\n'


@pytest.fixture
def remdir(tmp_path: Path, monkeypatch) -> Path:
    """Temp remediation dir wired into the helper via monkeypatch."""
    d = tmp_path / "remediation"
    d.mkdir()
    (d / "pe.yaml").write_text(_VALID_SPEC, encoding="utf-8")
    monkeypatch.setattr(rem, "REMEDIATION_DIR", d)
    return d


# ── pure-helper tests (the safety logic) ──────────────────────────────────────


class TestHelpers:
    def test_safe_path_rejects_separators_and_traversal(self, remdir):
        assert rem.safe_path("../pe.yaml") is None
        assert rem.safe_path("sub/pe.yaml") is None
        assert rem.safe_path("..") is None
        assert rem.safe_path("back\\slash.yaml") is None

    def test_safe_path_requires_yaml_extension(self, remdir):
        assert rem.safe_path("pe.txt") is None
        assert rem.safe_path("pe.py") is None  # cannot target engine.py etc.
        assert rem.safe_path("pe.yaml") is not None

    def test_list_files_filters_by_extension(self, remdir):
        (remdir / "__init__.py").write_text("", encoding="utf-8")
        (remdir / "README.md").write_text("", encoding="utf-8")
        files = rem.list_files()
        assert files == ["pe.yaml"]

    def test_normalise_appends_extension(self, remdir):
        assert rem.normalise_new_filename("agg") == ("agg.yaml", None)

    def test_normalise_rejects_empty_and_bad_chars(self, remdir):
        assert rem.normalise_new_filename("   ")[0] is None
        assert rem.normalise_new_filename("../evil")[0] is None

    def test_validate_valid_spec(self, remdir):
        assert rem.validate_content(_VALID_SPEC) is None

    def test_validate_empty_ok(self, remdir):
        assert rem.validate_content("") is None

    def test_validate_bad_match_rule(self, remdir):
        err = rem.validate_content(_BAD_MATCH)
        assert err
        assert "bogus" in err

    def test_validate_bad_yaml(self, remdir):
        err = rem.validate_content("add: : [")
        assert err
        assert "YAML syntax error" in err

    def test_validate_add_not_a_list(self, remdir):
        err = rem.validate_content("add: nope\n")
        assert err
        assert "list" in err

    def test_validate_legacy_allow_key_rejected_with_hint(self, remdir):
        err = rem.validate_content('allow:\n  - match: startswith\n    value: "x"\n')
        assert err
        assert "renamed to 'add'" in err

    def test_validate_add_may_not_carry_remediation_commands(self, remdir):
        bad = (
            'add:\n  - match: exact\n    value: "x"\n'
            "    remediation_commands:\n      - 'delete x'\n"
        )
        err = rem.validate_content(bad)
        assert err
        assert "remediation_commands" in err
        assert "add" in err

    def test_validate_delete_requires_non_empty_remediation_commands(self, remdir):
        err = rem.validate_content('delete:\n  - match: exact\n    value: "x"\n')
        assert err
        assert "remediation_commands" in err

    def test_validate_delete_remediation_commands_must_be_string_list(self, remdir):
        bad = 'delete:\n  - match: exact\n    value: "x"\n    remediation_commands: not-a-list\n'
        err = rem.validate_content(bad)
        assert err
        assert "remediation_commands" in err

    def test_validate_delete_bad_match(self, remdir):
        bad = 'delete:\n  - match: bogus\n    value: "x"\n    remediation_commands:\n      - "delete x"\n'
        err = rem.validate_content(bad)
        assert err
        assert "bogus" in err

    def test_validate_conditions_must_be_mapping(self, remdir):
        err = rem.validate_content("conditions: nope\n")
        assert err
        assert "conditions" in err

    def test_validate_delete_placeholder_with_named_group_ok(self, remdir):
        spec = (
            "delete:\n"
            "  - match: regex\n"
            r'    value: "subnet (?P<subnet>\\S+) options option 42"' + "\n"
            "    remediation_commands:\n"
            '      - "/configure delete subnet {subnet} options option 42"\n'
        )
        assert rem.validate_content(spec) is None

    def test_validate_delete_placeholder_without_named_group_rejected(self, remdir):
        # {subnet} referenced but the regex names no such group.
        spec = (
            "delete:\n"
            "  - match: regex\n"
            r'    value: "subnet \\S+ options"' + "\n"
            "    remediation_commands:\n"
            '      - "/configure delete subnet {subnet}"\n'
        )
        err = rem.validate_content(spec)
        assert err
        assert "{subnet}" in err
        assert "capture group" in err

    def test_validate_delete_placeholder_on_non_regex_rejected(self, remdir):
        # exact/startswith capture nothing, so any placeholder is unfillable.
        spec = (
            "delete:\n"
            "  - match: startswith\n"
            '    value: "configure x"\n'
            "    remediation_commands:\n"
            '      - "delete {foo}"\n'
        )
        err = rem.validate_content(spec)
        assert err
        assert "{foo}" in err


# ── route tests ───────────────────────────────────────────────────────────────


class TestAccess:
    def test_anonymous_redirected(self, anon_client):
        resp = anon_client.get("/compliance-remediations")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_index_renders(self, client, remdir):
        resp = client.get("/compliance-remediations")
        assert resp.status_code == 200
        assert b"Compliance remediations" in resp.data
        assert b"pe.yaml" in resp.data

    def test_ro_sees_readonly_view(self, login_as, remdir):
        resp = login_as(role="ro").get("/compliance-remediations/pe.yaml/edit")
        assert resp.status_code == 200
        assert b"<textarea" not in resp.data
        assert b"<pre" in resp.data

    def test_rw_sees_editor(self, login_as, remdir):
        resp = login_as(role="rw").get("/compliance-remediations/pe.yaml/edit")
        assert resp.status_code == 200
        assert b"<textarea" in resp.data

    def test_ro_cannot_save(self, login_as, remdir):
        resp = login_as(role="ro").post(
            "/compliance-remediations/pe.yaml/edit", data={"content": "x"}
        )
        assert resp.status_code == 403

    def test_ro_cannot_create(self, login_as, remdir):
        resp = login_as(role="ro").post("/compliance-remediations/create", data={"filename": "agg"})
        assert resp.status_code == 403

    def test_ro_cannot_delete(self, login_as, remdir):
        resp = login_as(role="ro").post("/compliance-remediations/pe.yaml/delete")
        assert resp.status_code == 403


class TestEditSave:
    def test_valid_save_writes_file(self, login_as, remdir):
        new = _VALID_SPEC.replace("configure router bgp", "configure router isis")
        resp = login_as(role="rw").post(
            "/compliance-remediations/pe.yaml/edit", data={"content": new}
        )
        assert resp.status_code == 200
        assert b"Saved successfully" in resp.data
        assert "configure router isis" in (remdir / "pe.yaml").read_text(encoding="utf-8")

    def test_invalid_rejected_and_file_untouched(self, login_as, remdir):
        before = (remdir / "pe.yaml").read_text(encoding="utf-8")
        resp = login_as(role="rw").post(
            "/compliance-remediations/pe.yaml/edit", data={"content": _BAD_MATCH}
        )
        assert resp.status_code == 200
        assert b"bogus" in resp.data
        assert (remdir / "pe.yaml").read_text(encoding="utf-8") == before

    def test_crlf_normalised_to_lf(self, login_as, remdir):
        crlf = _VALID_SPEC.replace("\n", "\r\n")
        resp = login_as(role="rw").post(
            "/compliance-remediations/pe.yaml/edit", data={"content": crlf}
        )
        assert resp.status_code == 200
        assert b"\r\n" not in (remdir / "pe.yaml").read_bytes()

    def test_edit_unknown_file_404(self, login_as, remdir):
        resp = login_as(role="rw").get("/compliance-remediations/nope.yaml/edit")
        assert resp.status_code == 404

    def test_save_bad_extension_404(self, login_as, remdir):
        resp = login_as(role="rw").post(
            "/compliance-remediations/evil.txt/edit", data={"content": "x"}
        )
        assert resp.status_code == 404


class TestCreate:
    def test_create_file(self, login_as, remdir):
        resp = login_as(role="rw").post("/compliance-remediations/create", data={"filename": "agg"})
        assert resp.status_code == 200
        assert (remdir / "agg.yaml").exists()
        assert (remdir / "agg.yaml").read_text(encoding="utf-8") == ""

    def test_create_duplicate_rejected(self, login_as, remdir):
        resp = login_as(role="rw").post(
            "/compliance-remediations/create", data={"filename": "pe.yaml"}
        )
        assert resp.status_code == 200
        assert b"already exists" in resp.data

    def test_create_bad_filename_rejected(self, login_as, remdir):
        resp = login_as(role="rw").post(
            "/compliance-remediations/create", data={"filename": "../evil"}
        )
        assert resp.status_code == 200
        assert b"Invalid filename" in resp.data
        assert not (remdir / "evil.yaml").exists()


class TestDelete:
    def test_delete_file(self, login_as, remdir):
        assert (remdir / "pe.yaml").exists()
        resp = login_as(role="rw").post("/compliance-remediations/pe.yaml/delete")
        assert resp.status_code == 200
        assert not (remdir / "pe.yaml").exists()

    def test_delete_unknown_file_404(self, login_as, remdir):
        resp = login_as(role="rw").post("/compliance-remediations/nope.yaml/delete")
        assert resp.status_code == 404
