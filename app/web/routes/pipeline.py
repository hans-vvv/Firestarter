"""Flask blueprint for triggering the full data pipeline from the dashboard.

The pipeline (validate Excel → seed → build topology → compute services →
snapshot) is the explicit, operator-initiated step that turns an updated input
workbook or edited service definitions into freshly computed configuration. It
runs synchronously; on failure the full traceback is surfaced in a modal so the
operator can see exactly what broke without digging through server logs.
"""

from __future__ import annotations

import contextlib

from flask import Blueprint, Response, render_template

from app.web.audit import set_audit_context
from app.web.auth import can_write
from app.web.utils import get_pipeline_last_run, run_data_pipeline

bp = Blueprint("pipeline", __name__)


@bp.get("/pipeline")
def index() -> str:
    return render_template(
        "pipeline.html",
        can_write=can_write(),
        last_run=get_pipeline_last_run(),
        just_ran=False,
    )


@bp.post("/pipeline/run")
def run() -> str | Response:
    if not can_write():
        return Response("Forbidden — rw or admin role required.", status=403)

    # run_data_pipeline records success/failure (incl. the traceback) in the
    # last-run cache and logs failures; we only need to render the outcome. The
    # failure is suppressed here so a broken run stays a normal 200 htmx swap —
    # the result partial (with the error modal) replaces the target instead of
    # erroring the swap.
    with contextlib.suppress(Exception):
        run_data_pipeline()

    last_run = get_pipeline_last_run()
    # The route returns 200 even on failure (the error modal is swapped in), so the
    # real outcome only lives in the last-run record — surface it to the audit line.
    set_audit_context(event="pipeline.run", result=last_run.status or "unknown")

    return render_template(
        "partials/pipeline_result.html",
        last_run=last_run,
        just_ran=True,
    )
