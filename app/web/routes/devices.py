"""Flask blueprint for device management routes."""

from __future__ import annotations

import io
import zipfile

from flask import Blueprint, render_template, request, send_file
from flask.typing import ResponseReturnValue

from app.domain.file_locations import ARTIFACTS_LOC
from app.web.utils import (
    filter_devices,
    list_devices,
    render_all_to_disk,
    render_device,
)

bp = Blueprint("devices", __name__)

# Mirrors the constant in app/printing/printer.py; resolved under the live data root.
_LATEST_DIR = ARTIFACTS_LOC.path / "latest"


@bp.get("/devices")
def index() -> str:
    devices = list_devices()
    return render_template("devices.html", devices=devices, total=len(devices), is_filtered=False)


@bp.get("/devices/rows")
def rows() -> str:
    """htmx partial — returns filtered table rows and an OOB count badge update."""
    hostname = request.args.get("hostname", "").strip()
    role = request.args.get("role", "")
    site = request.args.get("site", "")
    model = request.args.get("model", "")
    status = request.args.get("status", "")

    all_devices = list_devices()
    filtered = filter_devices(
        all_devices,
        hostname=hostname,
        role=role,
        site=site,
        model=model,
        status=status,
    )
    is_filtered = any([hostname, role, site, model, status])

    return render_template(
        "partials/device_rows.html",
        devices=filtered,
        total=len(all_devices),
        is_filtered=is_filtered,
    )


@bp.get("/devices/<hostname>/config")
def config(hostname: str) -> str:
    """htmx partial — returns rendered config as a pre block.

    Returns a friendly error card on any render failure (e.g. device
    model not in TEMPLATE_MAP, missing DB record) so a single
    unmodelled device never crashes the whole page.
    """
    try:
        cfg = render_device(hostname=hostname)
        return render_template("partials/config.html", hostname=hostname, config=cfg)
    except Exception as exc:
        return render_template("partials/config_error.html", hostname=hostname, reason=str(exc))


@bp.post("/devices/generate")
def generate() -> str:
    """htmx partial — renders all device configs to disk and reports the result."""
    rendered = render_all_to_disk()
    return render_template(
        "partials/generate_result.html",
        count=len(rendered),
        hostnames=sorted(rendered.keys()),
    )


@bp.get("/devices/download")
def download() -> ResponseReturnValue:
    """Stream a ZIP of all .cfg files from the latest render to the browser."""
    cfg_files = sorted(_LATEST_DIR.glob("*.cfg"))

    if not cfg_files:
        return "No configs found. Generate configs first.", 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for cfg_path in cfg_files:
            zf.write(cfg_path, arcname=cfg_path.name)
    buf.seek(0)

    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name="configs.zip",
    )
