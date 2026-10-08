from __future__ import annotations

"""Tests for the Latest backups blueprint and its join helper.

The join helper (list_latest_backups) is tested by monkeypatching list_devices +
the backup store, so no DB or on-disk run is needed. Route tests monkeypatch the
facade functions the blueprint imports, mirroring the devices-blueprint tests.
"""

import pytest

from app.automation.backup import FetchResult
from app.web.utils import TaskOutcome

_DEVICE = {
    "id": 1,
    "hostname": "core2.tst-001",
    "status": "active",
    "role": "pe",
    "site": "tst-001",
    "model_name": "7750-SR-1",
    "lag_name": "",
    "labels": {},
}


def _row(**over):
    row = {**_DEVICE, "backup_status": "ok", "backup_error": None}
    row.update(over)
    return row


# ── join helper ───────────────────────────────────────────────────────────────


class TestListLatestBackups:
    def test_ok_missing_and_failed_are_classified(self, monkeypatch):
        import app.web.utils as utils

        devices = [
            {**_DEVICE, "hostname": "ok.lab", "id": 1},
            {**_DEVICE, "hostname": "fail.lab", "id": 2},
            {**_DEVICE, "hostname": "gone.lab", "id": 3},
        ]
        results = {
            "ok.lab": FetchResult(hostname="ok.lab", raw="cfg\n"),
            "fail.lab": FetchResult(hostname="fail.lab", raw=None, error="timeout"),
            # gone.lab absent → missing
        }
        monkeypatch.setattr(utils, "list_devices", lambda: devices)
        monkeypatch.setattr(utils.backup, "load_results", lambda *, backups_dir: results)
        monkeypatch.setattr(
            utils.backup, "read_timestamp", lambda *, backups_dir: "2026-08-22T00:00:00"
        )

        rows, ts = utils.list_latest_backups()
        by_host = {r["hostname"]: r for r in rows}

        assert ts == "2026-08-22T00:00:00"
        assert by_host["ok.lab"]["backup_status"] == "ok"
        assert by_host["fail.lab"]["backup_status"] == "failed"
        assert by_host["fail.lab"]["backup_error"] == "timeout"
        assert by_host["gone.lab"]["backup_status"] == "missing"

    def test_preserves_device_filter_fields(self, monkeypatch):
        import app.web.utils as utils

        monkeypatch.setattr(utils, "list_devices", lambda: [_DEVICE])
        monkeypatch.setattr(utils.backup, "load_results", lambda *, backups_dir: {})
        monkeypatch.setattr(utils.backup, "read_timestamp", lambda *, backups_dir: None)

        rows, ts = utils.list_latest_backups()
        assert ts is None
        assert rows[0]["role"] == "pe"
        assert rows[0]["site"] == "tst-001"
        assert rows[0]["model_name"] == "7750-SR-1"


# ── routes ──────────────────────────────────────────────────────────────────--


def _patch_index(monkeypatch, rows, ts="2026-08-22T00:00:00"):
    monkeypatch.setattr("app.web.routes.latest_backups.list_latest_backups", lambda: (rows, ts))


class TestIndex:
    def test_anonymous_redirected(self, anon_client):
        resp = anon_client.get("/backups")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_index_renders_with_timestamp_and_host(self, client, monkeypatch):
        _patch_index(monkeypatch, [_row()])
        resp = client.get("/backups")
        assert resp.status_code == 200
        assert b"Latest backups" in resp.data
        assert b"core2.tst-001" in resp.data
        assert b"2026-08-22T00:00:00" in resp.data
        assert b"backed up" in resp.data

    def test_admin_sees_fetch_button_without_credentials_dialog(self, client, monkeypatch):
        _patch_index(monkeypatch, [_row()])
        resp = client.get("/backups")
        assert b"Fetch backups from simulated devices" in resp.data
        assert b"dlCredsModal" not in resp.data
        assert b'name="password"' not in resp.data

    def test_ro_does_not_see_fetch_button(self, login_as, monkeypatch):
        _patch_index(monkeypatch, [_row()])
        resp = login_as(role="ro").get("/backups")
        assert resp.status_code == 200
        assert b"Fetch backups from simulated devices" not in resp.data


class TestRows:
    def test_unfiltered_returns_all(self, client, monkeypatch):
        _patch_index(monkeypatch, [_row()])
        resp = client.get("/backups/rows")
        assert resp.status_code == 200
        assert b"core2.tst-001" in resp.data

    def test_role_filter_excludes_non_match(self, client, monkeypatch):
        _patch_index(monkeypatch, [_row()])
        resp = client.get("/backups/rows?role=core")
        assert b"core2.tst-001" not in resp.data
        assert b"1 of 1" not in resp.data  # nothing matched

    def test_status_filter_matches(self, client, monkeypatch):
        _patch_index(monkeypatch, [_row()])
        resp = client.get("/backups/rows?status=active")
        assert b"core2.tst-001" in resp.data

    def test_missing_status_badge_rendered(self, client, monkeypatch):
        _patch_index(monkeypatch, [_row(backup_status="missing")])
        resp = client.get("/backups/rows")
        assert b"no backup" in resp.data

    def test_row_carries_radio_select_not_hover(self, client, monkeypatch):
        # Backup loads on radio change, not on hover — sweeping the list must not
        # thrash the panel. Guards against a regression back to mouseenter.
        _patch_index(monkeypatch, [_row()])
        body = client.get("/backups/rows").data.decode()
        assert 'type="radio"' in body
        assert 'name="device-select"' in body
        assert 'hx-trigger="change"' in body
        assert "mouseenter" not in body


class TestConfigPanel:
    def test_ok_shows_config(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.latest_backups.latest_backup",
            lambda *, hostname: FetchResult(hostname=hostname, raw="configure system\n"),
        )
        resp = client.get("/backups/core2.tst-001/config")
        assert resp.status_code == 200
        assert b"configure system" in resp.data
        assert b"latest backup" in resp.data

    def test_failed_shows_error_card(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.latest_backups.latest_backup",
            lambda *, hostname: FetchResult(hostname=hostname, raw=None, error="timeout"),
        )
        resp = client.get("/backups/core2.tst-001/config")
        assert resp.status_code == 200
        assert b"fetch failed" in resp.data
        assert b"timeout" in resp.data

    def test_missing_shows_no_record_card(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.latest_backups.latest_backup", lambda *, hostname: None)
        resp = client.get("/backups/core2.tst-001/config")
        assert resp.status_code == 200
        assert b"No backup on record" in resp.data


class TestDownload:
    def test_ro_forbidden(self, login_as, monkeypatch):
        called = {"n": 0}

        def _boom():
            called["n"] += 1
            raise AssertionError("a read-only user must not trigger a fetch")

        monkeypatch.setattr("app.web.routes.latest_backups.download_device_backups", _boom)
        resp = login_as(role="ro").post("/backups/download")
        assert resp.status_code == 403
        assert called["n"] == 0

    def test_success_fetches_and_refreshes_table(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.latest_backups.download_device_backups",
            lambda: TaskOutcome(
                name="Fetch backups from simulated devices",
                ok=True,
                detail="1/1 simulated device(s) fetched",
            ),
        )
        _patch_index(monkeypatch, [_row()])
        resp = client.post("/backups/download")
        assert resp.status_code == 200
        assert b"1/1 simulated device(s) fetched" in resp.data
        assert b"core2.tst-001" in resp.data  # table refreshed
