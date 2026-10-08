"""Flask blueprint for compliance dashboard routes."""

from __future__ import annotations

from datetime import UTC, datetime

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue

from app.web.audit import set_audit_context
from app.web.auth import can_write, is_admin
from app.web.utils import (
    TaskOutcome,
    compliance_report_text,
    download_device_backups,
    pe_batch_digest,
    rebuild_nornir_inventory,
    remediate_device,
    remediate_pe_devices,
    remediation_candidate_views,
    run_compliance,
)

bp = Blueprint("compliance", __name__)


@bp.get("/compliance")
def index() -> str:
    return render_template("compliance.html", can_write=can_write(), is_admin=is_admin())


@bp.post("/compliance/run")
def run() -> str | Response:
    """htmx partial — optionally refreshes the live side, then runs compliance.

    The two tick-boxes are refresh steps for what compliance compares *against*,
    not separate features: the inventory decides which devices get backed up, and
    the backups are the live side of the diff. So they run in that order —
    inventory, then backups, then the comparison — and a compliance run with both
    ticked is a full end-to-end refresh. The backups come from the simulated
    device layer, so no credentials are involved.

    A failed task does not abort the run. The outcome is rendered alongside the
    results, and compliance still reports against whatever backups are on disk;
    telling the operator "the fetch failed, here is the comparison against the
    previous set" is more useful than showing nothing.
    """
    fetch_backups = request.form.get("fetch_backups") == "on"
    rebuild_inventory = request.form.get("rebuild_inventory") == "on"

    if (fetch_backups or rebuild_inventory) and not can_write():
        return Response("Forbidden — rw or admin role required.", status=403)

    tasks: list[TaskOutcome] = []

    if rebuild_inventory:
        tasks.append(rebuild_nornir_inventory())

    if fetch_backups:
        tasks.append(download_device_backups())

    summary = run_compliance()
    return render_template("partials/compliance_results.html", summary=summary, tasks=tasks)


@bp.get("/compliance/report")
def report() -> ResponseReturnValue:
    """Download the detailed text report for the most recent compliance run.

    Formats the run cached by ``/compliance/run`` (no re-run), so the
    download matches the table the operator is looking at. If no run has
    happened this process — e.g. a fresh server or after a restart — there is
    nothing to report, so we flash and send the operator back to run one.
    """
    text = compliance_report_text()
    if text is None:
        flash("Run a compliance check first, then download its report.", "warning")
        return redirect(url_for("compliance.index"))

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    filename = f"compliance_report_{ts}.txt"
    return Response(
        text,
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@bp.get("/compliance/remediation")
def remediation() -> str:
    """htmx partial — the remediation candidates projected from the last run.

    A pure read of the cached compliance run: every ``only_in_rendered`` line a
    per-role spec allow-lists, grouped by device and flagged eligible or blocked.
    The push button is admin-only; ``is_admin`` gates whether it is rendered.
    """
    return render_template(
        "partials/remediation_candidates.html",
        candidates=remediation_candidate_views(),
        pe_batch_digest=pe_batch_digest(),
        is_admin=is_admin(),
    )


@bp.post("/compliance/remediate")
def remediate() -> str | Response:
    """htmx partial — show what remediating one device would push. Admin-only.

    The lines are re-derived server-side from the cached run and the current spec
    (see :func:`app.web.utils.remediate_device`); the browser posts the hostname and
    the digest of the lines it was shown, and the result is refused if the
    re-derivation no longer matches that digest. Pushing to a device is disabled in
    this demo: any credentials the modal posts are ignored and never stored, and no
    device is contacted. Reuses the snippet-deploy result partial.
    """
    if not is_admin():
        return Response("Forbidden — administrator access required.", status=403)

    hostname = request.form.get("hostname", "").strip()
    presented_digest = request.form.get("digest", "").strip()

    result = remediate_device(hostname=hostname, presented_digest=presented_digest)
    set_audit_context(
        event="compliance.remediate",
        device=hostname,
        result="error" if result.error else "ok",
    )
    return render_template("partials/snippet_deploy_result.html", result=result)


@bp.post("/compliance/remediate-pe")
def remediate_pe() -> str | Response:
    """htmx partial — show what remediating every eligible pe would push. Admin-only.

    A batch over :func:`app.web.utils.remediate_pe_devices`: the eligible pe set
    (role ``pe``) and each device's lines are re-derived server-side from the cached
    run and the current specs, so the browser posts only the digest of the batch plan it
    previewed — never hostnames or config text. The batch is refused if the re-derived
    plan no longer matches that digest. Pushing to a device is disabled in this demo:
    the partial renders one stub result block per device and no device is contacted.
    """
    if not is_admin():
        return Response("Forbidden — administrator access required.", status=403)

    presented_digest = request.form.get("digest", "").strip()

    outcome = remediate_pe_devices(presented_digest=presented_digest)
    set_audit_context(
        event="compliance.remediate_pe",
        devices=len(outcome.results),
        result="digest_mismatch" if outcome.digest_mismatch else "ok",
    )
    return render_template(
        "partials/remediation_batch_result.html",
        results=outcome.results,
        digest_mismatch=outcome.digest_mismatch,
    )
