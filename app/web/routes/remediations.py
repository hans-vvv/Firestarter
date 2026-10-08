"""Flask blueprint for managing compliance-remediation spec files.

Lets operators create, edit and delete the per-role remediation specs the
remediation engine reads — ``app/compliance/remediation/<role>.yaml`` (allow
rules, ``remediation_commands``, ``conditions``; see ADR 0004) — from the
State → Compliance remediations page, instead of editing files on the server by
hand.

All mutating routes require the ``rw`` or ``admin`` role (the shared
``can_write`` gate). Read-only users may view spec contents but not change them.
Path safety lives entirely in ``app.web.remediations.safe_path`` — a filename
that does not resolve to a child of the remediation directory is treated as a
404, so the routes never touch a path outside it. Spec YAML is validated by
compiling its allow rules through the real matcher grammar before it is allowed
to land on disk; a half-written or malformed file can never reach the directory
because writes are atomic (temp file + os.replace).
"""

from __future__ import annotations

from flask import Blueprint, Response, render_template, request

from app.web.auth import can_write
from app.web.remediations import (
    PLACEHOLDER,
    atomic_write,
    list_files,
    normalise_new_filename,
    safe_path,
    validate_content,
)

bp = Blueprint("remediations", __name__)


def _render_files(**extra: object) -> str:
    """Render the file-list + create-form partial (the htmx swap target)."""
    return render_template(
        "partials/remediation_files.html",
        files=list_files(),
        placeholder=PLACEHOLDER,
        can_write=can_write(),
        **extra,
    )


@bp.get("/compliance-remediations")
def index() -> str:
    return render_template("remediations.html", render_files=_render_files)


@bp.get("/compliance-remediations/<filename>/edit")
def editor(filename: str) -> str | Response:
    path = safe_path(filename)
    if path is None or not path.exists():
        return Response("Not found.", status=404)
    content = path.read_text(encoding="utf-8")
    return render_template(
        "partials/remediation_editor.html",
        filename=filename,
        content=content,
        can_write=can_write(),
        error=None,
        saved=False,
    )


@bp.post("/compliance-remediations/<filename>/edit")
def save(filename: str) -> str | Response:
    if not can_write():
        return Response("Forbidden — rw or admin role required.", status=403)

    path = safe_path(filename)
    if path is None or not path.exists():
        return Response("Not found.", status=404)

    content = request.form.get("content", "")
    error = validate_content(content)
    if error is not None:
        return render_template(
            "partials/remediation_editor.html",
            filename=filename,
            content=content,
            can_write=True,
            error=error,
            saved=False,
        )

    atomic_write(path, content)
    return render_template(
        "partials/remediation_editor.html",
        filename=filename,
        content=content,
        can_write=True,
        error=None,
        saved=True,
    )


@bp.post("/compliance-remediations/create")
def create() -> str | Response:
    if not can_write():
        return Response("Forbidden — rw or admin role required.", status=403)

    filename, error = normalise_new_filename(request.form.get("filename", ""))
    if error is not None:
        return _render_files(error=error)

    path = safe_path(filename)
    if path is None:  # defensive — normalise_new_filename already validated it
        return _render_files(error="Invalid filename.")
    if path.exists():
        return _render_files(error=f"'{filename}' already exists.")

    atomic_write(path, "")
    return _render_files(created=filename)


@bp.post("/compliance-remediations/<filename>/delete")
def delete(filename: str) -> str | Response:
    if not can_write():
        return Response("Forbidden — rw or admin role required.", status=403)

    path = safe_path(filename)
    if path is None or not path.exists():
        return Response("Not found.", status=404)

    path.unlink()
    return _render_files(deleted=filename)
