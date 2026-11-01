from __future__ import annotations

"""Smoke tests for the compliance blueprint.

Routes covered:
  GET  /compliance       — landing page (no run yet)
  POST /compliance/run   — htmx partial with results table
"""

from unittest.mock import MagicMock


def _make_summary(compliant=1, non_compliant=0):
    """Return a minimal ComplianceRunSummary stand-in."""
    summary = MagicMock()
    summary.compliant_count = compliant
    summary.non_compliant_count = non_compliant
    summary.failed_fetch = []
    summary.skipped = []
    summary.results = {}
    return summary


# ---------------------------------------------------------------------------
# GET /compliance
# ---------------------------------------------------------------------------


class TestComplianceIndex:
    def test_returns_200(self, client):
        assert client.get("/compliance").status_code == 200


# ---------------------------------------------------------------------------
# POST /compliance/run
# ---------------------------------------------------------------------------


class TestComplianceRun:
    def test_returns_200(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.compliance.run_compliance",
            lambda **_: _make_summary(),
        )

        assert client.post("/compliance/run").status_code == 200

    def test_compliant_count_in_response(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.compliance.run_compliance",
            lambda **_: _make_summary(compliant=3, non_compliant=1),
        )

        body = client.post("/compliance/run").data.decode()

        assert "3" in body  # compliant_count badge
        assert "1" in body  # non_compliant_count badge

    def test_run_never_triggers_a_device_fetch(self, client, monkeypatch):
        """Compliance compares against the last backup; it must not reach devices.

        Fetching prompts for credentials and touches every device, so it stays a
        deliberate shell step. A stray form field must not revive it.
        """
        received = {}

        def _capture(**kwargs):
            received.update(kwargs)
            return _make_summary()

        monkeypatch.setattr("app.web.routes.compliance.run_compliance", _capture)

        client.post("/compliance/run", data={"get_latest": "on"})

        assert "get_latest" not in received

    def test_results_partial_has_download_link(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.compliance.run_compliance",
            lambda **_: _make_summary(),
        )

        body = client.post("/compliance/run").data.decode()

        assert "/compliance/report" in body


# ---------------------------------------------------------------------------
# GET /compliance/report
# ---------------------------------------------------------------------------


class TestComplianceReportDownload:
    def test_downloads_cached_report_as_attachment(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.compliance.compliance_report_text",
            lambda: "COMPLIANCE REPORT — detailed body",
        )

        resp = client.get("/compliance/report")

        assert resp.status_code == 200
        assert resp.mimetype == "text/plain"
        assert "attachment" in resp.headers["Content-Disposition"]
        assert "compliance_report_" in resp.headers["Content-Disposition"]
        assert b"detailed body" in resp.data

    def test_redirects_when_no_run_cached(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.compliance.compliance_report_text",
            lambda: None,
        )

        resp = client.get("/compliance/report")

        assert resp.status_code == 302
        assert "/compliance" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# POST /compliance/run — optional refresh tasks
# ---------------------------------------------------------------------------


class TestComplianceRefreshTasks:
    """The two tick-boxes that refresh what compliance compares against."""

    @staticmethod
    def _patch(monkeypatch, calls):
        from app.web.utils import TaskOutcome

        monkeypatch.setattr("app.web.routes.compliance.run_compliance", lambda **_: _make_summary())

        def _rebuild():
            calls.append("rebuild")
            return TaskOutcome(name="Rebuild Nornir inventory", ok=True, detail="19 device(s)")

        def _download():
            calls.append("download")
            return TaskOutcome(
                name="Fetch backups from simulated devices", ok=True, detail="19/19 fetched"
            )

        monkeypatch.setattr("app.web.routes.compliance.rebuild_nornir_inventory", _rebuild)
        monkeypatch.setattr("app.web.routes.compliance.download_device_backups", _download)

    def test_no_boxes_ticked_runs_neither_task(self, client, monkeypatch):
        calls = []
        self._patch(monkeypatch, calls)

        assert client.post("/compliance/run").status_code == 200
        assert calls == []

    def test_rebuild_box_rebuilds_the_inventory(self, client, monkeypatch):
        calls = []
        self._patch(monkeypatch, calls)

        client.post("/compliance/run", data={"rebuild_inventory": "on"})

        assert calls == ["rebuild"]

    def test_backup_box_fetches_without_credentials(self, client, monkeypatch):
        """The demo's devices are simulated: no username/password is needed or read."""
        calls = []
        self._patch(monkeypatch, calls)

        client.post("/compliance/run", data={"fetch_backups": "on"})

        assert calls == ["download"]

    def test_inventory_is_rebuilt_before_backups_are_fetched(self, client, monkeypatch):
        """The inventory decides which devices get backed up, so order matters."""
        calls = []
        self._patch(monkeypatch, calls)

        client.post("/compliance/run", data={"fetch_backups": "on", "rebuild_inventory": "on"})

        assert calls == ["rebuild", "download"]

    def test_task_outcome_is_shown_with_the_results(self, client, monkeypatch):
        self._patch(monkeypatch, [])

        body = client.post("/compliance/run", data={"rebuild_inventory": "on"}).data.decode()

        assert "Rebuild Nornir inventory" in body
        assert "19 device(s)" in body

    def test_failed_task_still_reports_compliance(self, client, monkeypatch):
        """A failed fetch must not hide the comparison against the previous set."""
        from app.web.utils import TaskOutcome

        monkeypatch.setattr(
            "app.web.routes.compliance.run_compliance",
            lambda **_: _make_summary(compliant=7),
        )
        monkeypatch.setattr(
            "app.web.routes.compliance.download_device_backups",
            lambda: TaskOutcome(
                name="Fetch backups from simulated devices", ok=False, detail="inventory missing"
            ),
        )

        body = client.post("/compliance/run", data={"fetch_backups": "on"}).data.decode()

        assert "inventory missing" in body
        assert "7 compliant" in body

    def test_read_only_user_may_still_run_compliance(self, login_as, monkeypatch):
        """Comparing is read-only; only refreshing the live side is gated."""
        monkeypatch.setattr("app.web.routes.compliance.run_compliance", lambda **_: _make_summary())

        assert login_as(role="ro").post("/compliance/run").status_code == 200

    def test_read_only_user_may_not_touch_devices(self, login_as, monkeypatch):
        calls = []
        self._patch(monkeypatch, calls)

        resp = login_as(role="ro").post("/compliance/run", data={"fetch_backups": "on"})

        assert resp.status_code == 403
        assert calls == []


class TestCredentialsDialogWiring:
    """The remediation modal is useless if its script cannot see bootstrap or htmx.

    base.html renders `block content` and only loads bootstrap.bundle.min.js and
    htmx.min.js *after* it, so a script placed in the content block runs against
    undefined globals: `new bootstrap.Modal` throws and the htmx:confirm listener
    is never registered. It must go in `block scripts`. The backup fetch itself has
    no dialog any more — the simulated devices need no credentials.
    """

    def test_script_runs_after_bootstrap_and_htmx(self, client):
        body = client.get("/compliance").data.decode()

        assert body.index("new bootstrap.Modal") > body.index("bootstrap.bundle.min.js")
        assert body.index("new bootstrap.Modal") > body.index("htmx.min.js")

    def test_backup_fetch_asks_for_no_credentials(self, client):
        body = client.get("/compliance").data.decode()
        assert "credsModal" not in body
        assert 'name="username"' not in body.split("remediateCredsModal")[0]
        assert "Fetch backups from simulated devices" in body

    def test_read_only_user_gets_no_dialog(self, login_as):
        body = login_as(role="ro").get("/compliance").data.decode()
        assert "Modal" not in body


# ---------------------------------------------------------------------------
# POST /compliance/run — results table ordering and skipped-device omission
# ---------------------------------------------------------------------------


def _real_summary(*, results, skipped):
    """A real ComplianceRunSummary carrying DiffResults and a skipped list.

    MagicMock is fine for the count-badge smoke tests, but ordering and the
    ``result.compliant`` branch need genuine objects the template can sort and
    read attributes off.
    """
    from app.compliance.differ import DiffResult
    from app.compliance.runner import ComplianceRunSummary

    summary = ComplianceRunSummary()
    for hostname, compliant in results:
        summary.results[hostname] = DiffResult(
            hostname=hostname,
            only_in_rendered=frozenset() if compliant else frozenset({"line-a"}),
            only_in_live=frozenset(),
            ignored_rendered=0,
            ignored_live=0,
        )
    summary.skipped = list(skipped)
    return summary


class TestResultsTableOrdering:
    def test_non_compliant_devices_are_listed_first(self, client, monkeypatch):
        """A non-compliant device must sort ahead of a compliant one, regardless
        of hostname, so the rows that need attention are at the top."""
        summary = _real_summary(
            # "aaa" is compliant, "zzz" is not: alphabetical order would put the
            # compliant one first, so this proves the sort is by status.
            results=[("aaa-compliant", True), ("zzz-broken", False)],
            skipped=[],
        )
        monkeypatch.setattr("app.web.routes.compliance.run_compliance", lambda **_: summary)

        body = client.post("/compliance/run").data.decode()

        assert body.index("zzz-broken") < body.index("aaa-compliant")

    def test_skipped_devices_are_not_shown(self, client, monkeypatch):
        """Skipped devices (typically not-yet-active, no live backup) are omitted
        entirely — no row, no badge, no filter option."""
        summary = _real_summary(
            results=[("real-device", True)],
            skipped=["not-active-device"],
        )
        monkeypatch.setattr("app.web.routes.compliance.run_compliance", lambda **_: summary)

        body = client.post("/compliance/run").data.decode()

        assert "not-active-device" not in body
        assert "Skipped" not in body
        assert 'value="skipped"' not in body
