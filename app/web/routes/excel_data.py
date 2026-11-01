"""Flask blueprint for downloading and replacing the input Excel workbook.

The dashboard exposes the single input workbook (``TOPOLOGY_EXCEL_LOC``) for
download by any authenticated user, and lets ``rw``/``admin`` users upload a
replacement. An uploaded file is checked with the same Excel input validation
the ingestion pipeline uses; only a workbook that passes is swapped in, and the
swap is atomic (temp file + ``os.replace``), so a malformed upload can never
land on disk and a concurrent download can never see a half-written file.

Uploading does NOT run the ingestion pipeline — it only refreshes the file on
disk. Topology ingestion stays an explicit, separate step.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime

from flask import (
    Blueprint,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask.typing import ResponseReturnValue

from app.web.audit import set_audit_context
from app.web.auth import can_write
from app.web.utils import (
    topology_excel_path,
    validate_excel_workbook,
)

bp = Blueprint("excel_data", __name__)


def _stat_info(path) -> dict | None:
    """Display metadata for a workbook on disk, or None if it is absent."""
    if not path.exists():
        return None
    stat = path.stat()
    return {
        "name": path.name,
        "size_kb": f"{stat.st_size / 1024:.1f}",
        "modified": datetime.fromtimestamp(stat.st_mtime, tz=UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }


def _file_info() -> dict | None:
    """Display metadata for the current input workbook (topology.xlsx)."""
    return _stat_info(topology_excel_path())


@bp.get("/excel-data")
def index() -> str:
    return render_template(
        "excel_data.html",
        file_info=_file_info(),
        can_write=can_write(),
        errors=None,
        crash_traceback=None,
    )


@bp.get("/excel-data/download")
def download() -> ResponseReturnValue:
    path = topology_excel_path()
    if not path.exists():
        flash("No input workbook is present on the server.", "danger")
        return redirect(url_for("excel_data.index"))
    return send_file(path, as_attachment=True, download_name=path.name)


@bp.post("/excel-data/upload")
def upload() -> ResponseReturnValue:
    if not can_write():
        return Response("Forbidden — rw or admin role required.", status=403)

    file = request.files.get("file")
    if file is None or not file.filename:
        flash("No file selected.", "danger")
        return redirect(url_for("excel_data.index"))

    if not file.filename.lower().endswith(".xlsx"):
        flash("Only .xlsx workbooks are accepted.", "danger")
        return redirect(url_for("excel_data.index"))

    dest = topology_excel_path()
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Stage the upload in a temp file in the destination directory so a workbook
    # that passes validation can be promoted with a single atomic rename (which
    # requires the temp file to be on the same filesystem as the destination).
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, suffix=".xlsx.tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            file.save(fh)

        result = validate_excel_workbook(wb_path=tmp_name)
        if not result.ok:
            set_audit_context(
                event="excel.upload", filename=file.filename, result="rejected_invalid"
            )
            return render_template(
                "excel_data.html",
                file_info=_file_info(),
                can_write=True,
                errors=result.errors,
                crash_traceback=result.crash_traceback,
            )

        os.replace(tmp_name, dest)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)

    set_audit_context(event="excel.upload", filename=file.filename, result="ok")
    flash(f"Workbook '{file.filename}' validated and saved.", "success")
    return redirect(url_for("excel_data.index"))
