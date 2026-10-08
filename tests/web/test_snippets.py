from __future__ import annotations

"""Tests for the snippets blueprint and its facade.

Routes covered:
  GET  /snippets                 — page with the device dropdown
  GET  /snippets/instances       — htmx service / named-object checkbox partial
  POST /snippets/render          — htmx rendered-snippet partial (success + error)
  POST /snippets/deploy          — htmx push partial (admin gate, guards, delegation)

Also covers token parsing + marginal-diff semantics of render_service_snippet,
and the deploy_service_snippet orchestration (max-2 guard, address lookup,
delegation), with fakes so the logic is verified without a database or a device.
"""

import contextlib
from types import SimpleNamespace

import pytest

from app.automation.deploy import DeployResult
from app.web import utils as web_utils
from app.web.push_guard import DIGEST_MISMATCH, command_digest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DEVICE = {
    "id": 1,
    "hostname": "pe1.tst-001",
    "status": "active",
    "role": "pe",
    "site": "tst-001",
    "model_name": "7750-SR-1x-48d",
    "lag_name": None,
    "labels": {},
}

# The picker only shows VPRN/VPLS instances now, each carrying named objects.
_VPRN_ITEM = {
    "id": 15,
    "svc_name": "vprn",
    "tenant": "lab",
    "variant": "lab",
    "objects": [
        {
            "ctx_key": "vprn",
            "variant_name": "lab",
            "object_name": "CUST-A-2130",
            "token": "obj|15|vprn|lab|CUST-A-2130",
        },
        {
            "ctx_key": "vprn",
            "variant_name": "lab",
            "object_name": "CUST-B-2140",
            "token": "obj|15|vprn|lab|CUST-B-2140",
        },
    ],
}
_VPLS_ITEM = {
    "id": 20,
    "svc_name": "evpn_vpls",
    "tenant": "lab",
    "variant": "lab",
    "objects": [
        {
            "ctx_key": "evpn_vpls",
            "variant_name": "lab",
            "object_name": "CUST-VPLS-40000",
            "token": "obj|20|evpn_vpls|lab|CUST-VPLS-40000",
        },
    ],
}


# ---------------------------------------------------------------------------
# GET /snippets
# ---------------------------------------------------------------------------


class TestSnippetsIndex:
    def test_returns_200(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.snippets.list_devices", lambda: [])
        assert client.get("/snippets").status_code == 200

    def test_device_appears_in_dropdown(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.snippets.list_devices", lambda: [_DEVICE])

        body = client.get("/snippets").data.decode()

        assert "pe1.tst-001" in body


# ---------------------------------------------------------------------------
# GET /snippets/instances
# ---------------------------------------------------------------------------


class TestSnippetInstances:
    def test_no_hostname_prompts_for_device(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.snippets.list_device_service_snippet_items",
            lambda *, hostname: [],
        )

        body = client.get("/snippets/instances").data.decode()

        assert "Choose a device" in body

    def test_objects_rendered_as_checkboxes_in_a_dropdown(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.snippets.list_device_service_snippet_items",
            lambda *, hostname: [_VPRN_ITEM],
        )

        body = client.get("/snippets/instances?hostname=pe1.tst-001").data.decode()

        assert "dropdown-menu" in body
        assert "CUST-A-2130" in body
        assert "CUST-B-2140" in body
        assert 'value="obj|15|vprn|lab|CUST-A-2130"' in body
        assert 'name="items"' in body

    def test_objects_grouped_by_service_instance(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.snippets.list_device_service_snippet_items",
            lambda *, hostname: [_VPRN_ITEM, _VPLS_ITEM],
        )

        body = client.get("/snippets/instances?hostname=pe1.tst-001").data.decode()

        # A dropdown header per service instance.
        assert "vprn" in body
        assert "evpn_vpls" in body
        assert "CUST-VPLS-40000" in body

    def test_no_services_message(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.snippets.list_device_service_snippet_items",
            lambda *, hostname: [],
        )

        body = client.get("/snippets/instances?hostname=pe1.tst-001").data.decode()

        assert "No VPRN / VPLS services" in body


# ---------------------------------------------------------------------------
# POST /snippets/render
# ---------------------------------------------------------------------------


class TestSnippetRender:
    def test_selected_lines_appear(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.snippets.render_service_snippet",
            lambda *, hostname, selection: ["/configure x", "/configure y"],
        )

        response = client.post(
            "/snippets/render",
            data={
                "hostname": "pe1.tst-001",
                "items": [
                    "obj|15|vprn|lab|CUST-A-2130",
                    "obj|15|vprn|lab|CUST-B-2140",
                ],
            },
        )

        assert response.status_code == 200
        body = response.data.decode()
        assert "/configure x" in body
        assert "/configure y" in body
        assert "2 services selected" in body

    def test_zero_selection_prompts_to_select(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.snippets.render_service_snippet",
            lambda *, hostname, selection: [],
        )

        body = client.post("/snippets/render", data={"hostname": "pe1.tst-001"}).data.decode()

        assert "Select at least one service" in body

    def test_render_error_shows_error_card(self, client, monkeypatch):
        def _boom(*, hostname, selection):
            raise KeyError("model not in TEMPLATE_MAP")

        monkeypatch.setattr("app.web.routes.snippets.render_service_snippet", _boom)

        response = client.post(
            "/snippets/render",
            data={"hostname": "pe1.tst-001", "items": ["inst|10"]},
        )

        assert response.status_code == 200
        assert b"Config not available" in response.data


class TestSnippetRenderDeployButton:
    """The Deploy control only exists for an admin, and only with lines to push."""

    def _patch_lines(self, monkeypatch, lines):
        monkeypatch.setattr(
            "app.web.routes.snippets.render_service_snippet",
            lambda *, hostname, selection: lines,
        )

    def test_admin_sees_deploy_form(self, client, monkeypatch):
        self._patch_lines(monkeypatch, ["/configure x"])
        body = client.post(
            "/snippets/render",
            data={"hostname": "pe1.tst-001", "items": ["obj|15|vprn|v|A"]},
        ).data.decode()
        assert 'id="deploy-form"' in body
        assert "Deploy to pe1.tst-001" in body
        # The digest of the shown lines is carried in the form for the push to verify.
        assert command_digest(["/configure x"]) in body

    def test_non_admin_sees_no_deploy_form(self, login_as, monkeypatch):
        self._patch_lines(monkeypatch, ["/configure x"])
        ro = login_as(role="ro")
        body = ro.post(
            "/snippets/render",
            data={"hostname": "pe1.tst-001", "items": ["obj|15|vprn|v|A"]},
        ).data.decode()
        assert "deploy-form" not in body

    def test_over_limit_disables_the_button(self, client, monkeypatch):
        self._patch_lines(monkeypatch, ["/configure x"])
        body = client.post(
            "/snippets/render",
            data={
                "hostname": "pe1.tst-001",
                "items": ["obj|1|vprn|v|A", "obj|2|vprn|v|B", "obj|3|vprn|v|C"],
            },
        ).data.decode()
        assert "disabled" in body
        assert "at most" in body


# ---------------------------------------------------------------------------
# POST /snippets/deploy — admin gate + delegation
# ---------------------------------------------------------------------------


class TestSnippetDeployRoute:
    def test_non_admin_is_forbidden(self, login_as, monkeypatch):
        # rw is a write role but NOT admin; deploying to live kit is admin-only.
        called = []
        monkeypatch.setattr(
            "app.web.routes.snippets.deploy_service_snippet",
            lambda **kw: called.append(kw),
        )
        rw = login_as(role="rw")
        resp = rw.post(
            "/snippets/deploy",
            data={"hostname": "r1", "items": ["obj|1|vprn|v|A"], "username": "u", "password": "p"},
        )
        assert resp.status_code == 403
        assert called == []  # never reached the device layer

    def test_admin_happy_path_renders_result(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.snippets.deploy_service_snippet",
            lambda **kw: DeployResult(hostname=kw["hostname"], ok=True, lines_pushed=2),
        )
        resp = client.post(
            "/snippets/deploy",
            data={"hostname": "r1", "items": ["obj|1|vprn|v|A"], "username": "u", "password": "p"},
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "committed and confirmed" in body
        assert "2" in body

    def test_selection_and_digest_are_passed_through(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "app.web.routes.snippets.deploy_service_snippet",
            lambda **kw: seen.update(kw) or DeployResult(hostname=kw["hostname"], ok=True),
        )
        client.post(
            "/snippets/deploy",
            data={
                "hostname": "r1",
                "items": ["obj|1|vprn|v|A", "obj|2|vprn|v|B"],
                "digest": "abc123",
                "username": "netops",
                "password": "s3cret",
            },
        )
        assert seen["hostname"] == "r1"
        assert seen["selection"] == ["obj|1|vprn|v|A", "obj|2|vprn|v|B"]
        assert seen["presented_digest"] == "abc123"
        # Push is disabled in the demo: credentials are never handed on.
        assert "username" not in seen
        assert "password" not in seen


# ---------------------------------------------------------------------------
# deploy_service_snippet — guards + delegation (no DB, no device)
# ---------------------------------------------------------------------------


class TestDeployServiceSnippet:
    """The demo stub: every production guard still applies, and nothing is sent."""

    def _patch(self, monkeypatch, *, lines):
        monkeypatch.setattr(web_utils, "render_service_snippet", lambda **kw: lines)

    def test_empty_selection_is_rejected_before_rendering(self, monkeypatch):
        monkeypatch.setattr(
            web_utils, "render_service_snippet", lambda **kw: pytest.fail("should not render")
        )
        result = web_utils.deploy_service_snippet(
            hostname="r1", selection=[], presented_digest="unused"
        )
        assert result.ok is False
        assert "at least one" in result.error.lower()

    def test_over_limit_is_rejected_before_rendering(self, monkeypatch):
        monkeypatch.setattr(
            web_utils, "render_service_snippet", lambda **kw: pytest.fail("should not render")
        )
        selection = ["obj|1|vprn|v|A", "obj|2|vprn|v|B", "obj|3|vprn|v|C"]
        result = web_utils.deploy_service_snippet(
            hostname="r1", selection=selection, presented_digest="unused"
        )
        assert result.ok is False
        assert str(web_utils.MAX_DEPLOY_SERVICES) in result.error
        assert "3 selected" in result.error

    def test_no_lines_reports_nothing_to_push(self, monkeypatch):
        self._patch(monkeypatch, lines=[])
        result = web_utils.deploy_service_snippet(
            hostname="r1", selection=["obj|1|vprn|v|A"], presented_digest="unused"
        )
        assert result.ok is False
        assert "nothing to push" in result.error.lower()

    def test_digest_mismatch_is_refused(self, monkeypatch):
        # The re-render no longer matches what the page showed (stale/tampered digest).
        self._patch(monkeypatch, lines=["/configure x"])
        result = web_utils.deploy_service_snippet(
            hostname="r1", selection=["obj|1|vprn|v|A"], presented_digest="stale-or-tampered"
        )
        assert result.ok is False
        assert result.error == DIGEST_MISMATCH

    def test_valid_push_is_stubbed_with_the_lines_and_a_notice(self, monkeypatch):
        self._patch(monkeypatch, lines=["/configure x", "/configure y"])
        result = web_utils.deploy_service_snippet(
            hostname="r1",
            selection=["obj|1|vprn|v|A"],
            presented_digest=command_digest(["/configure x", "/configure y"]),
        )
        assert result.ok is False
        assert result.error == web_utils.PUSH_DISABLED_NOTICE
        assert result.lines_pushed == 0
        # The lines a push would have committed are carried back for display.
        assert [e.command for e in result.exchanges] == ["/configure x\n/configure y"]
        assert all(not step.ok for step in result.steps if step.name == "Push to device")

    def test_stub_renders_the_notice_and_the_lines(self, client, monkeypatch):
        self._patch(monkeypatch, lines=["/configure x"])
        body = client.post(
            "/snippets/deploy",
            data={
                "hostname": "r1",
                "items": ["obj|1|vprn|v|A"],
                "digest": command_digest(["/configure x"]),
            },
        ).data.decode()
        assert web_utils.PUSH_DISABLED_NOTICE in body
        assert "/configure x" in body
        assert "committed and confirmed" not in body


# ---------------------------------------------------------------------------
# render_service_snippet — token parsing + marginal diff (fake Printer, no DB)
# ---------------------------------------------------------------------------


class _FakePrinter:
    """Canned render. Instance 9 contributes the 'isis' line; instance 5's VPRN
    objects 'A'/'B' contribute their own lines. Exclusions drop the matching
    lines so the diff recovers exactly the selected leaf's lines."""

    def __init__(self, *, session, exclude_instance_ids=None, exclude_objects=None):
        self.exc_inst = exclude_instance_ids or set()
        self.exc_obj = exclude_objects or {}

    def render_device(self, *, hostname):
        lines = ["/configure base", "/configure vprn A", "/configure vprn B", "/configure isis"]
        if 9 in self.exc_inst:
            lines = [ln for ln in lines if ln != "/configure isis"]
        for _ctx, _variant, name in self.exc_obj.get(5, []):
            lines = [ln for ln in lines if ln != f"/configure vprn {name}"]
        return "\n".join(lines)


class TestRenderServiceSnippet:
    def _patch(self, monkeypatch):
        monkeypatch.setattr(web_utils, "Printer", _FakePrinter)
        monkeypatch.setattr(web_utils, "db_session", lambda: contextlib.nullcontext(None))

    def test_single_object_token(self, monkeypatch):
        self._patch(monkeypatch)

        out = web_utils.render_service_snippet(hostname="r1", selection=["obj|5|vprn|v1|A"])

        assert out == ["/configure vprn A"]

    def test_whole_instance_token(self, monkeypatch):
        self._patch(monkeypatch)

        out = web_utils.render_service_snippet(hostname="r1", selection=["inst|9"])

        assert out == ["/configure isis"]

    def test_mixed_selection(self, monkeypatch):
        self._patch(monkeypatch)

        out = web_utils.render_service_snippet(
            hostname="r1", selection=["obj|5|vprn|v1|B", "inst|9"]
        )

        assert out == ["/configure isis", "/configure vprn B"]

    def test_empty_selection_yields_empty_without_rendering(self, monkeypatch):
        # Printer left unpatched on purpose — it must not be constructed.
        monkeypatch.setattr(web_utils, "db_session", lambda: contextlib.nullcontext(None))

        assert web_utils.render_service_snippet(hostname="r1", selection=[]) == []

    def test_unparseable_token_is_ignored(self, monkeypatch):
        monkeypatch.setattr(web_utils, "db_session", lambda: contextlib.nullcontext(None))

        assert web_utils.render_service_snippet(hostname="r1", selection=["garbage"]) == []


# ---------------------------------------------------------------------------
# _build_snippet_items — VPRN/VPLS-only filter (pure, no DB)
# ---------------------------------------------------------------------------


def _inst(inst_id, svc_name, computed):
    return SimpleNamespace(
        id=inst_id, svc_name=svc_name, tenant="lab", variant="default", computed=computed
    )


class TestBuildSnippetItems:
    def test_keeps_vprn_vpls_drops_underlay_and_esi(self):
        instances = [
            _inst(1, "isis", {"r1": {"isis": {"x": 1}}}),
            _inst(2, "bgp", {"r1": {"bgp": {"x": 1}}}),
            _inst(3, "sr", {"r1": {"sr": {"x": 1}}}),
            _inst(4, "evpn_esi", {"r1": {"evpn_esi": {"x": 1}}}),
            _inst(5, "vprn", {"r1": {"vprn": {"variant": {"v": {"VPRN-A": {}}}}}}),
            _inst(6, "evpn_vpls", {"r1": {"evpn_vpls": {"variant": {"v": {"VPLS-A": {}}}}}}),
        ]

        items = web_utils._build_snippet_items(instances, hostname="r1")

        assert {it["svc_name"] for it in items} == {"vprn", "evpn_vpls"}

    def test_object_token_shape(self):
        instances = [_inst(5, "vprn", {"r1": {"vprn": {"variant": {"v": {"VPRN-A": {}}}}}})]

        items = web_utils._build_snippet_items(instances, hostname="r1")

        assert items[0]["objects"][0]["token"] == "obj|5|vprn|v|VPRN-A"

    def test_instance_not_touching_host_is_excluded(self):
        instances = [_inst(5, "vprn", {"r2": {"vprn": {"variant": {"v": {"VPRN-A": {}}}}}})]

        assert web_utils._build_snippet_items(instances, hostname="r1") == []
