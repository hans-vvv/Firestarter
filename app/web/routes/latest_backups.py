"""Flask blueprint for the Latest backups page.

A device-centric view of the most recent backup run: the same devices the Device
list shows, with the same filters, each flagged with its latest-backup state and
its backed-up (live) config viewable in a side panel. The "Fetch backups from
simulated devices" action (rw/admin) refreshes the configs through
:func:`app.web.utils.download_device_backups` — the demo's simulated device layer,
so no credentials are asked for and no device is contacted. The retention prune
from the backup store applies automatically: old runs beyond the limit are dropped
as part of the same fetch.

The page is otherwise read-only: it reads whatever the last run wrote to
``backups/latest/`` under the data root.
"""

from __future__ import annotations

from flask import Blueprint, Response, render_template, request

from app.web.auth import can_write
from app.web.utils import (
    download_device_backups,
    filter_devices,
    latest_backup,
    list_latest_backups,
)

bp = Blueprint("latest_backups", __name__)


@bp.get("/backups")
def index() -> str:
    rows, timestamp = list_latest_backups()
    return render_template(
        "latest_backups.html",
        devices=rows,
        total=len(rows),
        timestamp=timestamp,
        is_filtered=False,
        can_write=can_write(),
    )


def _filtered_rows() -> tuple[list[dict], int, bool]:
    """Apply the shared device filters to the backup-augmented rows."""
    hostname = request.args.get("hostname", "").strip()
    role = request.args.get("role", "")
    site = request.args.get("site", "")
    model = request.args.get("model", "")
    status = request.args.get("status", "")

    all_rows, _ = list_latest_backups()
    filtered = filter_devices(
        all_rows, hostname=hostname, role=role, site=site, model=model, status=status
    )
    is_filtered = any([hostname, role, site, model, status])
    return filtered, len(all_rows), is_filtered


@bp.get("/backups/rows")
def rows() -> str:
    """htmx partial — filtered table rows and an OOB count badge update."""
    filtered, total, is_filtered = _filtered_rows()
    return render_template(
        "partials/backup_rows.html",
        devices=filtered,
        total=total,
        is_filtered=is_filtered,
    )


@bp.get("/backups/<hostname>/config")
def config(hostname: str) -> str:
    """htmx partial — the selected device's latest backed-up config.

    Three states: no record of the device in the last run, a recorded fetch
    failure, or a real config. Only the last renders the config; the other two
    render a friendly card so a missing/failed backup never looks like an empty
    config.
    """
    result = latest_backup(hostname=hostname)
    if result is None:
        return render_template(
            "partials/config_error.html",
            hostname=hostname,
            reason="No backup on record for this device in the latest run.",
        )
    if result.raw is None:
        return render_template(
            "partials/config_error.html",
            hostname=hostname,
            reason=f"The latest backup fetch failed: {result.error}",
        )
    return render_template("partials/backup_config.html", hostname=hostname, config=result.raw)


@bp.post("/backups/download")
def download() -> str | Response:
    """htmx partial — fetch fresh backups from the simulated devices, then refresh the table.

    Admin/rw only. No credentials: the demo's devices are simulated. The underlying
    backup run archives and prunes as usual, so a successful fetch also drops runs
    beyond the retention limit.
    """
    if not can_write():
        return Response("Forbidden — rw or admin role required.", status=403)

    outcome = download_device_backups()

    rows, timestamp = list_latest_backups()
    return render_template(
        "partials/backup_download_result.html",
        devices=rows,
        total=len(rows),
        is_filtered=False,
        timestamp=timestamp,
        outcome=outcome,
    )
