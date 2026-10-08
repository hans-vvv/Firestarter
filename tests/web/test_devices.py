from __future__ import annotations

"""Smoke tests for the devices blueprint.

Routes covered:
  GET  /devices                      — index page with device table
  GET  /devices/<hostname>/config    — htmx config partial (success + error)
  POST /devices/generate             — htmx generate partial
  GET  /devices/download             — ZIP download (with files / no files)
"""

import io
import zipfile

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEVICE = {
    "id": 1,
    "hostname": "r1.lab",
    "status": "active",
    "role": "core",
    "site": "tst-001",
    "model_name": "7750-SR-1",
    "lag_name": "Bundle-Ether",
    "labels": {},
}


# ---------------------------------------------------------------------------
# GET /devices
# ---------------------------------------------------------------------------


class TestDevicesIndex:
    def test_returns_200(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.devices.list_devices", lambda: [])
        assert client.get("/devices").status_code == 200

    def test_hostname_appears_in_table(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.devices.list_devices", lambda: [_DEVICE])

        body = client.get("/devices").data.decode()

        assert "r1.lab" in body


# ---------------------------------------------------------------------------
# GET /devices/<hostname>/config
# ---------------------------------------------------------------------------


class TestDevicesConfig:
    def test_success_returns_200_with_config(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.devices.render_device",
            lambda *, hostname: "## rendered config ##",
        )

        response = client.get("/devices/r1.lab/config")

        assert response.status_code == 200
        assert b"rendered config" in response.data

    def test_render_error_returns_200_with_error_card(self, client, monkeypatch):
        def _boom(*, hostname):
            raise KeyError("model not in TEMPLATE_MAP")

        monkeypatch.setattr("app.web.routes.devices.render_device", _boom)

        response = client.get("/devices/r1.lab/config")

        assert response.status_code == 200
        assert b"Config not available" in response.data

    def test_error_card_shows_hostname(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.devices.render_device",
            lambda *, hostname: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        body = client.get("/devices/r1.lab/config").data.decode()

        assert "r1.lab" in body


# ---------------------------------------------------------------------------
# POST /devices/generate
# ---------------------------------------------------------------------------


class TestDevicesGenerate:
    def test_returns_200(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.devices.render_all_to_disk",
            lambda: {"r1.lab": "config"},
        )

        assert client.post("/devices/generate").status_code == 200

    def test_hostname_listed_in_result(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.devices.render_all_to_disk",
            lambda: {"r1.lab": "cfg-r1", "r2.lab": "cfg-r2"},
        )

        body = client.post("/devices/generate").data.decode()

        assert "r1.lab" in body
        assert "r2.lab" in body


# ---------------------------------------------------------------------------
# GET /devices/rows  (htmx filter partial)
# ---------------------------------------------------------------------------


class TestDevicesRows:
    def test_returns_200(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.devices.list_devices", lambda: [])
        assert client.get("/devices/rows").status_code == 200

    def test_unfiltered_returns_all_devices(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.devices.list_devices", lambda: [_DEVICE])

        body = client.get("/devices/rows").data.decode()

        assert "r1.lab" in body

    def test_hostname_filter_matches_substring(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.devices.list_devices", lambda: [_DEVICE])

        body = client.get("/devices/rows?hostname=r1").data.decode()

        assert "r1.lab" in body

    def test_hostname_filter_excludes_non_match(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.devices.list_devices", lambda: [_DEVICE])

        body = client.get("/devices/rows?hostname=r99").data.decode()

        assert "r1.lab" not in body

    def test_role_filter_exact_match(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.devices.list_devices", lambda: [_DEVICE])

        body = client.get("/devices/rows?role=core").data.decode()

        assert "r1.lab" in body

    def test_role_filter_excludes_non_match(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.devices.list_devices", lambda: [_DEVICE])

        body = client.get("/devices/rows?role=edge").data.decode()

        assert "r1.lab" not in body

    def test_count_badge_shown_when_filtered(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.devices.list_devices", lambda: [_DEVICE])

        body = client.get("/devices/rows?role=core").data.decode()

        assert "of" in body  # "1 of 1" count badge

    def test_count_badge_absent_when_unfiltered(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.devices.list_devices", lambda: [_DEVICE])

        body = client.get("/devices/rows").data.decode()

        assert "of" not in body

    def test_no_match_message_shown(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.devices.list_devices", lambda: [_DEVICE])

        body = client.get("/devices/rows?hostname=zzz").data.decode()

        assert "No devices match" in body

    def test_row_carries_radio_select_not_hover(self, client, monkeypatch):
        # Config loads on radio change, not on hover — sweeping the list must not
        # thrash the panel. Guards against a regression back to mouseenter.
        monkeypatch.setattr("app.web.routes.devices.list_devices", lambda: [_DEVICE])

        body = client.get("/devices/rows").data.decode()

        assert 'type="radio"' in body
        assert 'name="device-select"' in body
        assert 'hx-trigger="change"' in body
        assert "mouseenter" not in body


# ---------------------------------------------------------------------------
# GET /devices/download
# ---------------------------------------------------------------------------


class TestDevicesDownload:
    def test_returns_zip_when_configs_exist(self, client, monkeypatch, tmp_path):
        cfg = tmp_path / "r1.lab.cfg"
        cfg.write_text("## config ##")

        monkeypatch.setattr("app.web.routes.devices._LATEST_DIR", tmp_path)

        response = client.get("/devices/download")

        assert response.status_code == 200
        assert response.content_type == "application/zip"

    def test_zip_contains_cfg_file(self, client, monkeypatch, tmp_path):
        cfg = tmp_path / "r1.lab.cfg"
        cfg.write_text("## config ##")

        monkeypatch.setattr("app.web.routes.devices._LATEST_DIR", tmp_path)

        response = client.get("/devices/download")

        with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
            assert "r1.lab.cfg" in zf.namelist()

    def test_returns_404_when_no_configs(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("app.web.routes.devices._LATEST_DIR", tmp_path)

        assert client.get("/devices/download").status_code == 404


# ---------------------------------------------------------------------------
# Offline assets — htmx must be vendored locally, not pulled from a CDN
#
# Regression guard: the dashboard runs on air-gapped networks. If htmx is
# loaded from a public CDN it silently fails to load there, and every
# hx-post control (Generate Configs, filters, …) becomes dead. These tests
# fail if anyone re-points htmx back at a CDN.
# ---------------------------------------------------------------------------


class TestOfflineAssets:
    def test_page_loads_htmx_from_local_static(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.devices.list_devices", lambda: [])

        body = client.get("/devices").data.decode()

        assert "/static/vendor/htmx.min.js" in body

    def test_page_does_not_load_htmx_from_cdn(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.devices.list_devices", lambda: [])

        body = client.get("/devices").data.decode()

        assert "jsdelivr.net/npm/htmx" not in body

    def test_vendored_htmx_is_served(self, client):
        response = client.get("/static/vendor/htmx.min.js")

        assert response.status_code == 200
        assert b"htmx" in response.data
