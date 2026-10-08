from __future__ import annotations

"""Smoke tests for the overview blueprint (GET /)."""


class TestOverviewIndex:
    def test_returns_200(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.overview.list_devices", lambda: [])
        monkeypatch.setattr("app.web.routes.overview.list_jobs", lambda: [])

        response = client.get("/")

        assert response.status_code == 200

    def test_counts_are_rendered(self, client, monkeypatch):
        devices = [
            {
                "hostname": "r1",
                "status": None,
                "role": None,
                "site": None,
                "model_name": None,
                "id": 1,
                "lag_name": None,
                "labels": {},
            }
        ]
        jobs = [
            {
                "id": 1,
                "name": "j1",
                "status": "completed",
                "created_at": None,
                "executed_at": None,
                "committed_at": None,
            },
            {
                "id": 2,
                "name": "j2",
                "status": "pending",
                "created_at": None,
                "executed_at": None,
                "committed_at": None,
            },
        ]
        monkeypatch.setattr("app.web.routes.overview.list_devices", lambda: devices)
        monkeypatch.setattr("app.web.routes.overview.list_jobs", lambda: jobs)

        response = client.get("/")
        body = response.data.decode()

        assert "1" in body  # device_count
        assert "2" in body  # job_count
