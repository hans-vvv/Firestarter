"""Flask blueprint for job management routes."""

from __future__ import annotations

from flask import Blueprint, render_template

from app.web.utils import list_jobs

bp = Blueprint("jobs", __name__)


@bp.get("/jobs")
def index() -> str:
    jobs = list_jobs()
    return render_template("jobs.html", jobs=jobs)
