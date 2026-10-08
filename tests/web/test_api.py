from __future__ import annotations

"""Tests for the JSON API blueprint.

Routes covered:
  GET /api/v1/devices           — JSON list of network devices (no CEs) with mgmt IP.
  GET /api/v1/ce-mgmt           — JSON list of CE management records.
  GET /api/v1/ce-mgmt/<host>    — one CE's management record (404 otherwise).

The API is deliberately unauthenticated, so these use ``anon_client`` (no
session) — proving it is exempt from the global login gate that redirects the
dashboard pages. Facade functions (``list_devices``, ``devices_with_mgmt_address``,
``ce_mgmt_report_rows``) are monkeypatched, matching the pattern in
``test_devices.py``.
"""


def _device(hostname, role, status="active"):
    # Only the keys the API reads; list_devices() returns more, but the route
    # projects down to these.
    return {"hostname": hostname, "role": role, "status": status}


def _report_row(ce_hostname, ce_role, **overrides):
    # Shape mirrors build_report_ce_mgmt()'s records; only the keys the route
    # reads matter, the rest of the report's columns are omitted for brevity.
    row = {
        "ce_hostname": ce_hostname,
        "ce_role": ce_role,
        "pe1": "pe1.lab",
        "port1": "1/1/c7/1",
        "pe2": "pe2.lab",
        "port2": "1/1/c7/1",
        "mgmt_ip": "10.202.0.6/28",
        "vrrp_gateway": "10.202.0.1",
    }
    row.update(overrides)
    return row


class TestDevicesApi:
    def test_returns_json_200_without_auth(self, anon_client, monkeypatch):
        monkeypatch.setattr("app.web.routes.api.list_devices", lambda: [])
        monkeypatch.setattr("app.web.routes.api.devices_with_mgmt_address", lambda: [])
        monkeypatch.setattr("app.web.routes.api.ce_mgmt_report_rows", lambda: [])

        resp = anon_client.get("/api/v1/devices")

        assert resp.status_code == 200
        assert resp.content_type == "application/json"
        assert resp.get_json() == []

    def test_projects_expected_shape_and_joins_mgmt_ip(self, anon_client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.api.list_devices",
            lambda: [_device("core1.lab", "core", status="active")],
        )
        monkeypatch.setattr(
            "app.web.routes.api.devices_with_mgmt_address",
            lambda: [("core1.lab", "10.102.6.4")],
        )
        monkeypatch.setattr("app.web.routes.api.ce_mgmt_report_rows", lambda: [])

        payload = anon_client.get("/api/v1/devices").get_json()

        assert payload == [
            {
                "hostname": "core1.lab",
                "role": "core",
                "mgmt_ip": "10.102.6.4",
                "status": "active",
            }
        ]

    def test_mgmt_ip_is_null_when_device_has_none(self, anon_client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.api.list_devices",
            lambda: [_device("nomgmt.lab", "core")],
        )
        monkeypatch.setattr("app.web.routes.api.devices_with_mgmt_address", lambda: [])
        monkeypatch.setattr("app.web.routes.api.ce_mgmt_report_rows", lambda: [])

        payload = anon_client.get("/api/v1/devices").get_json()

        assert payload[0]["mgmt_ip"] is None

    def test_excludes_ces(self, anon_client, monkeypatch):
        """A CE is recognised by having a CE-management record, not by its role."""
        monkeypatch.setattr(
            "app.web.routes.api.list_devices",
            lambda: [
                _device("core1.lab", "core"),
                _device("switch1.lab", "switch"),
                _device("switch2.lab", "test-switch"),
                _device("rr1.lab", "rr"),
            ],
        )
        monkeypatch.setattr("app.web.routes.api.devices_with_mgmt_address", lambda: [])
        monkeypatch.setattr(
            "app.web.routes.api.ce_mgmt_report_rows",
            lambda: [
                _report_row("switch1.lab", "switch"),
                _report_row("switch2.lab", "test-switch"),
            ],
        )

        roles = {d["role"] for d in anon_client.get("/api/v1/devices").get_json()}

        assert roles == {"core", "rr"}


class TestCeMgmtApi:
    def test_all_returns_every_ce_with_hostname_and_six_keys(self, anon_client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.api.ce_mgmt_report_rows",
            lambda: [
                _report_row("switch1.lab", "switch"),
                _report_row("switch2.lab", "test-switch"),
            ],
        )

        payload = anon_client.get("/api/v1/ce-mgmt").get_json()

        assert {r["hostname"] for r in payload} == {"switch1.lab", "switch2.lab"}
        assert set(payload[0]) == {
            "hostname",
            "pe1",
            "port1",
            "pe2",
            "port2",
            "mgmt_ip",
            "GW",
        }

    def test_one_returns_six_keys_and_maps_gateway(self, anon_client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.api.ce_mgmt_report_rows",
            lambda: [
                _report_row(
                    "switch2.lab",
                    "switch",
                    pe1="pe3.lab",
                    port1="1/1/c5/1",
                    pe2="pe3.lab",
                    port2="1/1/c6/1",
                    mgmt_ip="10.202.0.20/28",
                    vrrp_gateway="10.202.0.17",
                )
            ],
        )

        resp = anon_client.get("/api/v1/ce-mgmt/switch2.lab")

        assert resp.status_code == 200
        # Exactly the six-key contract — no hostname on the per-CE record — and
        # GW carries the report's vrrp_gateway.
        assert resp.get_json() == {
            "pe1": "pe3.lab",
            "port1": "1/1/c5/1",
            "pe2": "pe3.lab",
            "port2": "1/1/c6/1",
            "mgmt_ip": "10.202.0.20/28",
            "GW": "10.202.0.17",
        }

    def test_one_404_for_unknown_hostname(self, anon_client, monkeypatch):
        monkeypatch.setattr("app.web.routes.api.ce_mgmt_report_rows", lambda: [])

        resp = anon_client.get("/api/v1/ce-mgmt/nope.lab")

        assert resp.status_code == 404
        assert "error" in resp.get_json()
