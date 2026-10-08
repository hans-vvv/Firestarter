from __future__ import annotations

"""Tests for the remediation web layer (ADR 0004).

Routes:
  GET  /compliance/remediation  — the candidate list partial (read-only + admin push form)
  POST /compliance/remediate    — the admin-only push, delegating to remediate_device

Plus remediate_device orchestration (eligibility re-check, digest check, demo push
stub) with fakes so no database or device is touched.
"""

from app.automation.deploy import DeployResult
from app.compliance.remediation import DeviceRemediation
from app.web import utils as web_utils
from app.web.push_guard import DIGEST_MISMATCH, command_digest


def _cand(
    hostname,
    *,
    role="pe",
    eligible=True,
    reasons=(),
    lines=("configure router bgp",),
    remediation_commands=(),
):
    return DeviceRemediation(
        hostname=hostname,
        role_name=role,
        eligible=eligible,
        blocked_reasons=tuple(reasons),
        lines=tuple(lines),
        remediation_commands=tuple(remediation_commands),
    )


def _digest(cand):
    """The push-guard digest for one candidate — what the page carries and the push verifies."""
    return command_digest(web_utils.remediation_push_lines(cand))


# ---------------------------------------------------------------------------
# GET /compliance/remediation
# ---------------------------------------------------------------------------


class TestRemediationView:
    def test_empty_shows_placeholder(self, client, monkeypatch):
        monkeypatch.setattr("app.web.utils.last_remediation_candidates", lambda: [])
        body = client.get("/compliance/remediation").data.decode()
        assert "No remediation candidates" in body

    def test_eligible_candidate_rendered_with_lines(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.utils.last_remediation_candidates",
            lambda: [_cand("pe1", lines=("configure router bgp", "configure router isis"))],
        )
        body = client.get("/compliance/remediation").data.decode()
        assert "pe1" in body
        assert "Eligible" in body
        assert "configure router bgp" in body
        assert "configure router isis" in body

    def test_admin_sees_push_form(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.utils.last_remediation_candidates",
            lambda: [_cand("pe1")],
        )
        body = client.get("/compliance/remediation").data.decode()
        assert "remediate-form" in body
        assert "Remediate pe1" in body

    def test_non_admin_sees_no_push_form(self, login_as, monkeypatch):
        monkeypatch.setattr(
            "app.web.utils.last_remediation_candidates",
            lambda: [_cand("pe1")],
        )
        rw = login_as(role="rw")
        body = rw.get("/compliance/remediation").data.decode()
        assert "pe1" in body  # still visible, read-only
        assert "remediate-form" not in body

    def test_push_form_carries_the_per_device_digest(self, client, monkeypatch):
        cand = _cand("pe1")
        monkeypatch.setattr("app.web.utils.last_remediation_candidates", lambda: [cand])
        body = client.get("/compliance/remediation").data.decode()
        # The digest of the exact push lines is in the form for the push to verify.
        assert _digest(cand) in body

    def test_admin_sees_pe_batch_button(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.utils.last_remediation_candidates",
            lambda: [_cand("pe1"), _cand("pe2")],
        )
        body = client.get("/compliance/remediation").data.decode()
        assert "remediate-pe-form" in body
        assert "Remediate all pe devices (2)" in body

    def test_batch_form_carries_the_batch_digest(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.utils.last_remediation_candidates",
            lambda: [_cand("pe1"), _cand("pe2")],
        )
        expected = web_utils.pe_batch_digest()
        body = client.get("/compliance/remediation").data.decode()
        assert expected in body

    def test_batch_button_absent_when_only_non_pe_eligible(self, client, monkeypatch):
        # A non-pe eligible device is not a pe batch candidate, so no button.
        monkeypatch.setattr(
            "app.web.utils.last_remediation_candidates",
            lambda: [_cand("core1", role="core")],
        )
        body = client.get("/compliance/remediation").data.decode()
        assert "core1" in body  # still shown in the read-only table
        assert "remediate-pe-form" not in body
        assert "Remediate all pe devices" not in body

    def test_remediation_commands_rendered_as_removals(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.utils.last_remediation_candidates",
            lambda: [
                _cand(
                    "pe1",
                    lines=("configure router bgp neighbor new",),
                    remediation_commands=('delete router "Base" bgp neighbor "192.0.2.17"',),
                )
            ],
        )
        body = client.get("/compliance/remediation").data.decode()
        assert "Add to device" in body
        assert "Remove from device" in body
        # The delete is shown as a removal (~), the addition as a + line. Jinja
        # HTML-escapes the embedded quotes, so match on the quote-free structure.
        assert "~ delete router" in body
        assert "bgp neighbor" in body
        assert "192.0.2.17" in body
        # The additive line is shown exactly as it will be pushed — slash restored.
        assert "+ /configure router bgp neighbor new" in body

    def test_delete_only_candidate_shows_removals_without_additions(self, client, monkeypatch):
        # A device with stale config and no additions: the Remove section renders, the
        # Add section is suppressed (c.lines is empty).
        monkeypatch.setattr(
            "app.web.utils.last_remediation_candidates",
            lambda: [_cand("pe1", lines=(), remediation_commands=("delete system snmp",))],
        )
        body = client.get("/compliance/remediation").data.decode()
        assert "Remove from device" in body
        assert "~ delete system snmp" in body
        assert "Add to device" not in body

    def test_blocked_candidate_shows_reason_and_no_form(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.utils.last_remediation_candidates",
            lambda: [_cand("pe1", eligible=False, reasons=("status is planned, needs active",))],
        )
        body = client.get("/compliance/remediation").data.decode()
        assert "Blocked" in body
        assert "status is planned" in body
        assert "remediate-form" not in body


# ---------------------------------------------------------------------------
# POST /compliance/remediate — admin gate + delegation
# ---------------------------------------------------------------------------


class TestRemediateRoute:
    def test_non_admin_is_forbidden(self, login_as, monkeypatch):
        called = []
        monkeypatch.setattr(
            "app.web.routes.compliance.remediate_device", lambda **kw: called.append(kw)
        )
        rw = login_as(role="rw")
        resp = rw.post(
            "/compliance/remediate",
            data={"hostname": "pe1", "username": "u", "password": "p"},
        )
        assert resp.status_code == 403
        assert called == []

    def test_admin_happy_path_renders_result(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.compliance.remediate_device",
            lambda **kw: DeployResult(hostname=kw["hostname"], ok=True, lines_pushed=3),
        )
        resp = client.post(
            "/compliance/remediate",
            data={"hostname": "pe1", "username": "u", "password": "p"},
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "committed and confirmed" in body
        assert "pe1" in body

    def test_hostname_and_digest_passed_through(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "app.web.routes.compliance.remediate_device",
            lambda **kw: seen.update(kw) or DeployResult(hostname=kw["hostname"], ok=True),
        )
        client.post(
            "/compliance/remediate",
            data={
                "hostname": "pe1",
                "digest": "abc123",
                "username": "netops",
                "password": "s3cret",
            },
        )
        # Push is disabled in the demo: credentials are never handed on.
        assert seen == {"hostname": "pe1", "presented_digest": "abc123"}


# ---------------------------------------------------------------------------
# remediate_device — guards + delegation (no DB, no device)
# ---------------------------------------------------------------------------


class TestRemediateDevice:
    """The demo stub: every production guard still applies, and nothing is sent."""

    def test_no_candidate_is_reported(self, monkeypatch):
        monkeypatch.setattr(web_utils, "last_remediation_candidates", lambda: [])
        result = web_utils.remediate_device(hostname="pe1", presented_digest="unused")
        assert result.ok is False
        assert "no remediation candidates" in result.error.lower()

    def test_blocked_device_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            web_utils,
            "last_remediation_candidates",
            lambda: [_cand("pe1", eligible=False, reasons=("status is planned, needs active",))],
        )
        result = web_utils.remediate_device(hostname="pe1", presented_digest="unused")
        assert result.ok is False
        assert "not eligible" in result.error.lower()
        assert "status is planned" in result.error

    def test_digest_mismatch_is_refused(self, monkeypatch):
        # Eligible, but the presented digest does not match the re-derived lines —
        # what the page showed no longer equals what would be pushed.
        monkeypatch.setattr(web_utils, "last_remediation_candidates", lambda: [_cand("pe1")])
        result = web_utils.remediate_device(hostname="pe1", presented_digest="stale-or-tampered")
        assert result.ok is False
        assert result.error == DIGEST_MISMATCH

    def test_eligible_device_is_stubbed_with_slash_restored_lines(self, monkeypatch):
        cand = _cand("pe1", lines=("configure router bgp", "configure router isis"))
        monkeypatch.setattr(web_utils, "last_remediation_candidates", lambda: [cand])
        result = web_utils.remediate_device(hostname="pe1", presented_digest=_digest(cand))
        assert result.ok is False
        assert result.error == web_utils.PUSH_DISABLED_NOTICE
        # Additive lines are shown slash-prefixed, exactly as a push would send them.
        assert [e.command for e in result.exchanges] == [
            "/configure router bgp\n/configure router isis"
        ]

    def test_remediation_commands_appended_after_additive_lines(self, monkeypatch):
        # Additions (slash-restored) then the delete (verbatim), in one block.
        cand = _cand(
            "pe1",
            lines=("configure router bgp neighbor new",),
            remediation_commands=('delete router "Base" bgp neighbor "192.0.2.17"',),
        )
        monkeypatch.setattr(web_utils, "last_remediation_candidates", lambda: [cand])
        result = web_utils.remediate_device(hostname="pe1", presented_digest=_digest(cand))
        assert result.exchanges[0].command.split("\n") == [
            "/configure router bgp neighbor new",
            'delete router "Base" bgp neighbor "192.0.2.17"',
        ]

    def test_only_the_named_host_is_selected(self, monkeypatch):
        monkeypatch.setattr(
            web_utils,
            "last_remediation_candidates",
            lambda: [
                _cand("pe1", lines=("line A",)),
                _cand("pe2", lines=("line B",)),
            ],
        )
        result = web_utils.remediate_device(
            hostname="pe2", presented_digest=_digest(_cand("pe2", lines=("line B",)))
        )
        assert result.hostname == "pe2"
        assert result.exchanges[0].command == "/line B"


class TestPushCommand:
    def test_restores_leading_slash_on_normalised_line(self):
        assert web_utils._push_command("configure router bgp") == "/configure router bgp"

    def test_already_absolute_line_is_unchanged(self):
        assert web_utils._push_command("/configure router bgp") == "/configure router bgp"


# ---------------------------------------------------------------------------
# POST /compliance/remediate-pe — admin gate + delegation
# ---------------------------------------------------------------------------


class TestRemediatePeRoute:
    def test_non_admin_is_forbidden(self, login_as, monkeypatch):
        called = []
        monkeypatch.setattr(
            "app.web.routes.compliance.remediate_pe_devices", lambda **kw: called.append(kw)
        )
        rw = login_as(role="rw")
        resp = rw.post("/compliance/remediate-pe", data={"username": "u", "password": "p"})
        assert resp.status_code == 403
        assert called == []

    def test_admin_happy_path_renders_batch_summary(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.compliance.remediate_pe_devices",
            lambda **kw: web_utils.BatchRemediationResult(
                results=[
                    DeployResult(hostname="pe1", ok=True, lines_pushed=2),
                    DeployResult(hostname="pe2", ok=True, lines_pushed=1),
                ]
            ),
        )
        resp = client.post("/compliance/remediate-pe", data={"username": "u", "password": "p"})
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Remediated" in body
        assert "2</strong>" in body  # "2 of 2"
        assert "pe1" in body
        assert "pe2" in body

    def test_failures_are_surfaced_in_the_summary(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.compliance.remediate_pe_devices",
            lambda **kw: web_utils.BatchRemediationResult(
                results=[
                    DeployResult(hostname="pe1", ok=True, lines_pushed=2),
                    DeployResult(hostname="pe2", error="unreachable"),
                ]
            ),
        )
        body = client.post(
            "/compliance/remediate-pe", data={"username": "u", "password": "p"}
        ).data.decode()
        assert "1</strong>" in body  # "1 of 2"
        assert "2</strong>" in body
        assert "unreachable" in body

    def test_digest_mismatch_is_reported(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.compliance.remediate_pe_devices",
            lambda **kw: web_utils.BatchRemediationResult(results=[], digest_mismatch=True),
        )
        body = client.post(
            "/compliance/remediate-pe", data={"username": "u", "password": "p"}
        ).data.decode()
        assert "no longer matches what would be pushed" in body

    def test_digest_passed_through_without_credentials(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "app.web.routes.compliance.remediate_pe_devices",
            lambda **kw: seen.update(kw) or web_utils.BatchRemediationResult(results=[]),
        )
        client.post(
            "/compliance/remediate-pe",
            data={"digest": "abc123", "username": "netops", "password": "s3cret"},
        )
        # Push is disabled in the demo: credentials are never handed on.
        assert seen == {"presented_digest": "abc123"}


# ---------------------------------------------------------------------------
# remediate_pe_devices — eligible pe filter, lock, delegation (no device)
# ---------------------------------------------------------------------------


class TestRemediatePeDevices:
    """The demo stub: the batch plan is derived and digest-checked, nothing is sent."""

    @staticmethod
    def _plan(outcome):
        return {r.hostname: r.exchanges[0].command.split("\n") for r in outcome.results}

    def test_empty_when_nothing_eligible(self, monkeypatch):
        monkeypatch.setattr(
            web_utils,
            "last_remediation_candidates",
            lambda: [_cand("pe1", eligible=False, reasons=("blocked",))],
        )
        outcome = web_utils.remediate_pe_devices(presented_digest="unused")
        assert outcome.results == []
        assert outcome.digest_mismatch is False

    def test_digest_mismatch_refuses_the_batch(self, monkeypatch):
        monkeypatch.setattr(
            web_utils, "last_remediation_candidates", lambda: [_cand("pe1"), _cand("pe2")]
        )
        outcome = web_utils.remediate_pe_devices(presented_digest="stale-or-tampered")
        assert outcome.results == []
        assert outcome.digest_mismatch is True

    def test_non_pe_eligible_devices_are_excluded(self, monkeypatch):
        # An eligible device of some other role must never ride along in the pe
        # batch — the button is scoped to role pe, not to "everything eligible".
        monkeypatch.setattr(
            web_utils,
            "last_remediation_candidates",
            lambda: [
                _cand("pe1", lines=("line A",)),
                _cand("core1", role="core", lines=("line B",)),
                _cand("core1", role="core", eligible=True, lines=("line C",)),
            ],
        )
        outcome = web_utils.remediate_pe_devices(presented_digest=web_utils.pe_batch_digest())
        assert self._plan(outcome) == {"pe1": ["/line A"]}

    def test_empty_when_only_non_pe_are_eligible(self, monkeypatch):
        monkeypatch.setattr(
            web_utils,
            "last_remediation_candidates",
            lambda: [_cand("core1", role="core", lines=("line B",))],
        )
        assert web_utils.remediate_pe_devices(presented_digest="unused").results == []

    def test_only_eligible_hosts_and_their_lines_are_stubbed(self, monkeypatch):
        monkeypatch.setattr(
            web_utils,
            "last_remediation_candidates",
            lambda: [
                _cand("pe1", lines=("line A",)),
                _cand("pe2", eligible=False, reasons=("blocked",), lines=("line B",)),
                _cand("pe3", lines=("line C1", "line C2")),
            ],
        )
        outcome = web_utils.remediate_pe_devices(presented_digest=web_utils.pe_batch_digest())
        assert self._plan(outcome) == {
            "pe1": ["/line A"],
            "pe3": ["/line C1", "/line C2"],
        }
        assert all(r.error == web_utils.PUSH_DISABLED_NOTICE for r in outcome.results)
        assert not any(r.ok for r in outcome.results)

    def test_remediation_commands_are_included_per_host_in_the_batch(self, monkeypatch):
        monkeypatch.setattr(
            web_utils,
            "last_remediation_candidates",
            lambda: [
                _cand("pe1", lines=("line A",), remediation_commands=("delete A-old",)),
                _cand("pe2", lines=("line B",)),
            ],
        )
        outcome = web_utils.remediate_pe_devices(presented_digest=web_utils.pe_batch_digest())
        # Additive lines slash-restored; the delete rides along verbatim.
        assert self._plan(outcome) == {
            "pe1": ["/line A", "delete A-old"],
            "pe2": ["/line B"],
        }
