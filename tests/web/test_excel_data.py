from __future__ import annotations

"""Route tests for the Excel-data blueprint (download / validate-before-replace).

The workbook path is repointed at a temp file via the monkeypatched
``topology_excel_path`` facade so the routes never touch the real input workbook,
and so replacement can be asserted on disk. ``validate_excel_workbook`` runs for
real against the staged upload — the valid fixture is the repo's test workbook,
the invalid case is non-workbook bytes carrying an ``.xlsx`` name.
"""

from io import BytesIO
from pathlib import Path

import pytest

import app.web.routes.excel_data as excel_data
from app.validation.excel_input_checks import ValidationError

# The repo's test workbook is a known-valid input file (see conftest fixtures).
_VALID_XLSX = (Path(__file__).resolve().parents[1] / "test.xlsx").read_bytes()


@pytest.fixture
def topology(tmp_path: Path, monkeypatch) -> Path:
    """Temp workbook path wired into the route via the monkeypatched facade.

    Seeded with a small placeholder so download works and 'file untouched'
    assertions have a stable baseline to compare against.
    """
    path = tmp_path / "topology.xlsx"
    path.write_bytes(b"ORIGINAL")
    monkeypatch.setattr(excel_data, "topology_excel_path", lambda: path)
    return path


def _upload(client, content: bytes, filename: str = "new.xlsx"):
    return client.post(
        "/excel-data/upload",
        data={"file": (BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


class TestAccess:
    def test_anonymous_redirected(self, anon_client, topology):
        resp = anon_client.get("/excel-data")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_index_renders(self, client, topology):
        resp = client.get("/excel-data")
        assert resp.status_code == 200
        assert b"Excel data" in resp.data

    def test_ro_has_no_upload_form(self, login_as, topology):
        resp = login_as(role="ro").get("/excel-data")
        assert resp.status_code == 200
        assert b'name="file"' not in resp.data
        assert b"requires the" in resp.data  # rw/admin note

    def test_rw_sees_upload_form(self, login_as, topology):
        resp = login_as(role="rw").get("/excel-data")
        assert resp.status_code == 200
        assert b'name="file"' in resp.data


class TestDownload:
    def test_download_returns_file(self, client, topology):
        resp = client.get("/excel-data/download")
        assert resp.status_code == 200
        assert "attachment" in resp.headers["Content-Disposition"]
        assert resp.data == b"ORIGINAL"

    def test_ro_can_download(self, login_as, topology):
        resp = login_as(role="ro").get("/excel-data/download")
        assert resp.status_code == 200

    def test_download_missing_file_redirects(self, client, tmp_path, monkeypatch):
        missing = tmp_path / "absent.xlsx"
        monkeypatch.setattr(excel_data, "topology_excel_path", lambda: missing)
        resp = client.get("/excel-data/download")
        assert resp.status_code == 302


class TestUpload:
    def test_ro_cannot_upload(self, login_as, topology):
        resp = _upload(login_as(role="ro"), _VALID_XLSX)
        assert resp.status_code == 403
        assert topology.read_bytes() == b"ORIGINAL"  # untouched

    def test_valid_upload_replaces_file(self, login_as, topology):
        resp = _upload(login_as(role="rw"), _VALID_XLSX)
        assert resp.status_code == 302  # PRG redirect on success
        assert topology.read_bytes() == _VALID_XLSX

    def test_admin_can_upload(self, client, topology):
        resp = _upload(client, _VALID_XLSX)
        assert resp.status_code == 302
        assert topology.read_bytes() == _VALID_XLSX

    def test_invalid_upload_rejected_and_file_untouched(self, login_as, topology):
        resp = _upload(login_as(role="rw"), b"not a workbook")
        assert resp.status_code == 200
        assert b"Validation failed" in resp.data
        assert topology.read_bytes() == b"ORIGINAL"

    def test_invalid_upload_surfaces_errors_in_modal(self, login_as, topology):
        # Structured validation errors are shown in an auto-opening modal, not just
        # an inline block, so the operator cannot miss them after the page reload.
        resp = _upload(login_as(role="rw"), b"not a workbook")
        assert resp.status_code == 200
        assert b'id="excelErrorModal"' in resp.data
        assert b"Could not read workbook" in resp.data  # the error message itself
        assert b"getOrCreateInstance" in resp.data  # auto-open script present

    def test_unexpected_validation_error_shows_traceback(self, login_as, topology, monkeypatch):
        # A check that raises an *unexpected* exception must not 500 — the caught
        # traceback is surfaced in the modal and the current workbook is untouched.
        import app.web.utils as web_utils

        def _boom(**_kwargs):
            raise RuntimeError("kaboom in a validator")

        monkeypatch.setattr(web_utils, "validate_excel_inputs", _boom)

        resp = _upload(login_as(role="rw"), _VALID_XLSX)
        assert resp.status_code == 200
        assert b"Validation crashed" in resp.data
        assert b"full traceback" in resp.data
        assert b"kaboom in a validator" in resp.data
        assert topology.read_bytes() == b"ORIGINAL"

    def test_wrong_extension_rejected(self, login_as, topology):
        resp = _upload(login_as(role="rw"), _VALID_XLSX, filename="data.txt")
        assert resp.status_code == 302
        assert topology.read_bytes() == b"ORIGINAL"

    def test_no_file_redirects(self, login_as, topology):
        resp = login_as(role="rw").post(
            "/excel-data/upload", data={}, content_type="multipart/form-data"
        )
        assert resp.status_code == 302
        assert topology.read_bytes() == b"ORIGINAL"
