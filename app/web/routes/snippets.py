"""Flask blueprint for per-service config snippets.

Lets an operator pick a device and a subset of its service instances — or, for
VPRN/VPLS services, individual named objects (a single VPRN or VPLS) inside an
instance — and see exactly the configuration lines those contribute.

The contribution is computed as a marginal diff (see
``app.web.utils.render_service_snippet``): the device is rendered with every
service, then again with the chosen leaves excluded, and the lines present only
in the full render are returned. Viewing is read-only — gated solely by the
global login requirement. **Pushing** that snippet to the device (``/deploy``) is
a different matter: it changes live equipment, so it is admin-only.
"""

from __future__ import annotations

from flask import Blueprint, Response, render_template, request

from app.web.audit import set_audit_context
from app.web.auth import is_admin
from app.web.push_guard import command_digest
from app.web.utils import (
    MAX_DEPLOY_SERVICES,
    deploy_service_snippet,
    list_device_service_snippet_items,
    list_devices,
    render_service_snippet,
)

bp = Blueprint("snippets", __name__)


@bp.get("/snippets")
def index() -> str:
    devices = list_devices()
    return render_template("snippets.html", devices=devices, is_admin=is_admin())


@bp.get("/snippets/instances")
def instances() -> str:
    """htmx partial — service / named-object checkboxes for the chosen device."""
    hostname = request.args.get("hostname", "").strip()
    items = list_device_service_snippet_items(hostname=hostname) if hostname else []
    return render_template(
        "partials/snippet_instances.html",
        hostname=hostname,
        items=items,
    )


@bp.post("/snippets/render")
def render() -> str:
    """htmx partial — rendered snippet for the selected leaves.

    Returns the friendly error card on any render failure (e.g. device model
    not in TEMPLATE_MAP) so one unmodelled device never blanks the panel.
    """
    hostname = request.form.get("hostname", "").strip()
    selection = request.form.getlist("items")
    try:
        lines = render_service_snippet(hostname=hostname, selection=selection)
    except Exception as exc:
        return render_template("partials/config_error.html", hostname=hostname, reason=str(exc))
    return render_template(
        "partials/snippet_result.html",
        hostname=hostname,
        lines=lines,
        # Digest of the exact lines shown; the deploy form carries it so the push can
        # refuse if a re-render no longer matches what is displayed here.
        digest=command_digest(lines),
        selection=selection,
        selected_count=len(selection),
        is_admin=is_admin(),
        max_deploy=MAX_DEPLOY_SERVICES,
    )


@bp.post("/snippets/deploy")
def deploy() -> str | Response:
    """htmx partial — show what deploying the selected snippet would push. Admin-only.

    The configuration is re-derived server-side from the posted selection (see
    :func:`app.web.utils.deploy_service_snippet`); the browser never sends config
    text, only the selection and the digest of the lines it was shown, and the
    result is refused if the re-render no longer matches that digest. Pushing to a
    device is disabled in this demo: any credentials the modal posts are ignored
    and never stored, and no device is contacted.
    """
    if not is_admin():
        return Response("Forbidden — administrator access required.", status=403)

    hostname = request.form.get("hostname", "").strip()
    selection = request.form.getlist("items")
    presented_digest = request.form.get("digest", "").strip()

    result = deploy_service_snippet(
        hostname=hostname,
        selection=selection,
        presented_digest=presented_digest,
    )
    set_audit_context(
        event="config.deploy",
        device=hostname,
        items=len(selection),
        result="error" if result.error else "ok",
    )
    return render_template("partials/snippet_deploy_result.html", result=result)
