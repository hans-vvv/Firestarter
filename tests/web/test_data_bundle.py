from __future__ import annotations

"""Route tests for the data-bundle blueprint (download only).

The domain facades (build/preview) are monkeypatched on the route module so the
HTTP layer is tested in isolation — the build mechanics have their own unit tests
in ``tests/data_bundle``. What matters here is the login gate and the download
response shape.
"""

import pytest

import app.web.routes.data_bundle as data_bundle

_PREVIEW = {
    "environment": "test",
    "files": [{"path": "app.db", "size_kb": "12.0"}],
    "count": 1,
}


@pytest.fixture(autouse=True)
def _patch_facades(monkeypatch):
    """Stub the domain facades so routes never touch real files or the DB."""
    monkeypatch.setattr(data_bundle, "data_bundle_preview", lambda: dict(_PREVIEW))
    monkeypatch.setattr(data_bundle, "build_data_bundle", lambda: b"ZIP-BYTES")


class TestAccess:
    def test_anonymous_redirected(self, anon_client):
        resp = anon_client.get("/data-bundle")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_index_renders(self, client):
        resp = client.get("/data-bundle")
        assert resp.status_code == 200
        assert b"Data bundle" in resp.data

    def test_index_has_no_upload_form(self, client):
        # Download-only: there is no restore/upload control anywhere on the page.
        resp = client.get("/data-bundle")
        assert b'type="file"' not in resp.data
        assert b'name="file"' not in resp.data


class TestDownload:
    def test_download_returns_zip(self, client):
        resp = client.get("/data-bundle/download")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "application/zip"
        assert "attachment" in resp.headers["Content-Disposition"]
        assert ".zip" in resp.headers["Content-Disposition"]
        assert resp.data == b"ZIP-BYTES"

    def test_ro_can_download(self, login_as):
        resp = login_as(role="ro").get("/data-bundle/download")
        assert resp.status_code == 200
        assert resp.data == b"ZIP-BYTES"

    def test_upload_endpoint_is_gone(self, client):
        # The old restore endpoint must no longer exist.
        resp = client.post("/data-bundle/upload", data={})
        assert resp.status_code in (404, 405)
