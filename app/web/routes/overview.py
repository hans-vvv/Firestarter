"""Flask blueprint for the dashboard overview route."""

from __future__ import annotations

from flask import Blueprint, render_template

from app.web.utils import list_devices, list_jobs

bp = Blueprint("overview", __name__)


@bp.get("/")
def index() -> str:
    devices = list_devices()
    jobs = list_jobs()

    completed_jobs = [j for j in jobs if j["status"] == "completed"]
    pending_jobs = [j for j in jobs if j["status"] == "pending"]

    return render_template(
        "overview.html",
        device_count=len(devices),
        job_count=len(jobs),
        completed_jobs=len(completed_jobs),
        pending_jobs=len(pending_jobs),
    )
