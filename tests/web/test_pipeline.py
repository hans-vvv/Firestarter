from __future__ import annotations

"""Route tests for the data-pipeline blueprint.

The pipeline itself is mocked via the route module's ``run_data_pipeline`` /
``get_pipeline_last_run`` facades, so these tests exercise access control and
the success / failure rendering (incl. the traceback modal) without running the
real ingestion pipeline.
"""

import app.web.routes.pipeline as pipeline_route
from app.web.utils import PipelineLastRun


def _ok_run(*, job_name="do_all_2026-06-27_10-00-00"):
    from datetime import UTC, datetime

    return PipelineLastRun(
        status="ok",
        job_name=job_name,
        timestamp=datetime(2026, 6, 27, 10, 0, 0, tzinfo=UTC),
        duration_seconds=12.3,
    )


def _failed_run(tb="Traceback (most recent call last):\n  ValueError: boom"):
    from datetime import UTC, datetime

    return PipelineLastRun(
        status="failed",
        timestamp=datetime(2026, 6, 27, 10, 0, 0, tzinfo=UTC),
        traceback=tb,
    )


class TestAccess:
    def test_anonymous_redirected(self, anon_client):
        resp = anon_client.get("/pipeline")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_index_renders(self, client):
        resp = client.get("/pipeline")
        assert resp.status_code == 200
        assert b"Run data pipeline" in resp.data

    def test_ro_has_no_run_button(self, login_as):
        resp = login_as(role="ro").get("/pipeline")
        assert resp.status_code == 200
        assert b"hx-post" not in resp.data
        assert b"requires the" in resp.data  # rw/admin note

    def test_rw_sees_run_button(self, login_as):
        resp = login_as(role="rw").get("/pipeline")
        assert resp.status_code == 200
        assert b"hx-post" in resp.data


class TestRun:
    def test_ro_cannot_run(self, login_as, monkeypatch):
        called = []
        monkeypatch.setattr(pipeline_route, "run_data_pipeline", lambda: called.append(1))
        resp = login_as(role="ro").post("/pipeline/run")
        assert resp.status_code == 403
        assert called == []  # gate fires before the pipeline is touched

    def test_rw_run_success(self, login_as, monkeypatch):
        monkeypatch.setattr(pipeline_route, "run_data_pipeline", lambda: None)
        monkeypatch.setattr(pipeline_route, "get_pipeline_last_run", _ok_run)

        resp = login_as(role="rw").post("/pipeline/run")
        assert resp.status_code == 200
        assert b"Pipeline completed" in resp.data
        assert b"do_all_2026-06-27_10-00-00" in resp.data

    def test_admin_run_success(self, client, monkeypatch):
        monkeypatch.setattr(pipeline_route, "run_data_pipeline", lambda: None)
        monkeypatch.setattr(pipeline_route, "get_pipeline_last_run", _ok_run)

        resp = client.post("/pipeline/run")
        assert resp.status_code == 200
        assert b"Pipeline completed" in resp.data

    def test_run_failure_shows_traceback_in_modal(self, client, monkeypatch):
        def _boom():
            raise ValueError("boom")

        monkeypatch.setattr(pipeline_route, "run_data_pipeline", _boom)
        monkeypatch.setattr(pipeline_route, "get_pipeline_last_run", _failed_run)

        resp = client.post("/pipeline/run")
        # A failed run is still a 200 htmx swap, with the traceback + modal.
        assert resp.status_code == 200
        assert b"Pipeline failed" in resp.data
        assert b"pipelineErrorModal" in resp.data
        assert b"ValueError: boom" in resp.data
        # just_ran=True auto-opens the modal.
        assert b".show()" in resp.data
