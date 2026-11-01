"""Flask blueprint for downloading the production-data bundle.

The bundle is a ZIP of every file that is *environment-specific state* rather than
versioned code: the database, the input workbook, the production YAML definitions,
and the compliance reference/ignore files (see :mod:`app.data_bundle.spec`). It
lets an operator pull an environment's state out — to archive it, or to seed a
development worktree by unzipping it there.

Download only, on purpose: loading a bundle back is a filesystem step done where
you have shell access (``unzip`` at the repo root places every file at its manifest
path). There is deliberately no web upload — nothing can overwrite a live database
through the browser.
"""

from __future__ import annotations

from datetime import UTC, datetime

from flask import Blueprint, Response, render_template
from flask.typing import ResponseReturnValue

from app.web.utils import build_data_bundle, data_bundle_preview

bp = Blueprint("data_bundle", __name__)


@bp.get("/data-bundle")
def index() -> str:
    return render_template("data_bundle.html", preview=data_bundle_preview())


@bp.get("/data-bundle/download")
def download() -> ResponseReturnValue:
    preview = data_bundle_preview()
    data = build_data_bundle()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    filename = f"firestarter-data-{preview['environment']}-{stamp}.zip"
    return Response(
        data,
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
