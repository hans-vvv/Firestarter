from __future__ import annotations

"""Smoke tests for the jobs blueprint.

Routes covered:
  GET /jobs   — job list page
"""


_JOB = {
    "id": 1,
    "name": "add-pe-pair",
    "status": "pending",
    "created_at": None,
    "executed_at": None,
    "committed_at": None,
}


class TestJobsIndex:
    def test_returns_200(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.jobs.list_jobs", lambda: [])
        assert client.get("/jobs").status_code == 200

    def test_job_name_appears_in_page(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.jobs.list_jobs", lambda: [_JOB])

        body = client.get("/jobs").data.decode()

        assert "add-pe-pair" in body

    def test_empty_job_list_renders_without_error(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.jobs.list_jobs", lambda: [])

        response = client.get("/jobs")

        assert response.status_code == 200
