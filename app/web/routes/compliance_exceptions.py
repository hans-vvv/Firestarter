"""Flask blueprint for managing compliance-exception files.

Lets operators create, edit and delete the per-device exception files the
compliance normaliser reads — ``app/compliance/extra/*.cfg`` (extra config
lines) and ``app/compliance/ignore/*.yaml`` (ignore + redact rules) — from the
State → Compliance exceptions page, instead of editing files on the server by
hand.

All mutating routes require the ``rw`` or ``admin`` role (the shared
``can_write`` gate). Read-only users may view file contents but not change them.
Path safety lives entirely in ``app.web.compliance_exceptions.safe_path`` — a
filename that does not resolve to a child of the section directory is treated as
a 404, so the routes never touch a path outside the two managed directories.
Ignore-rule YAML is validated by compiling it through the real normaliser code
path before it is allowed to land on disk; a half-written or malformed file can
never reach the directory because writes are atomic (temp file + os.replace).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from flask import Blueprint, Response, render_template, request

from app.web.auth import can_write
from app.web.compliance_exceptions import (
    SECTIONS,
    is_protected,
    list_files,
    normalise_new_filename,
    safe_path,
    validate_content,
)

bp = Blueprint("compliance_exceptions", __name__)


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically (temp file + os.replace).

    A half-written exception file would feed the compliance run a corrupt rule
    set, so the new content is staged in a temp file on the same directory and
    swapped in with a single atomic rename.

    Line endings are normalised to ``\\n``: browsers submit textarea content
    with ``\\r\\n`` but the repo stores these files with ``\\n``, and writing the
    raw body back would flip every line in the git diff. ``newline=""`` disables
    further translation so exactly the normalised bytes are written.
    """
    normalised = content.replace("\r\n", "\n").replace("\r", "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(normalised)
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise


def _render_section(section_key: str, **extra: object) -> str:
    """Render one section's file-list partial (the htmx swap target)."""
    sec = SECTIONS[section_key]
    files = list_files(section_key)
    return render_template(
        "partials/compliance_exception_section.html",
        section=section_key,
        meta=sec,
        files=files,
        protected={f for f in files if is_protected(section_key, f)},
        can_write=can_write(),
        **extra,
    )


@bp.get("/compliance-exceptions")
def index() -> str:
    return render_template(
        "compliance_exceptions.html",
        sections=SECTIONS,
        render_section=_render_section,
    )


@bp.get("/compliance-exceptions/<section>/<filename>/edit")
def editor(section: str, filename: str) -> str | Response:
    path = safe_path(section, filename)
    if path is None or not path.exists():
        return Response("Not found.", status=404)
    content = path.read_text(encoding="utf-8")
    return render_template(
        "partials/compliance_exception_editor.html",
        section=section,
        filename=filename,
        content=content,
        can_write=can_write(),
        error=None,
        saved=False,
    )


@bp.post("/compliance-exceptions/<section>/<filename>/edit")
def save(section: str, filename: str) -> str | Response:
    if not can_write():
        return Response("Forbidden — rw or admin role required.", status=403)

    path = safe_path(section, filename)
    if path is None or not path.exists():
        return Response("Not found.", status=404)

    content = request.form.get("content", "")
    error = validate_content(section, content)
    if error is not None:
        return render_template(
            "partials/compliance_exception_editor.html",
            section=section,
            filename=filename,
            content=content,
            can_write=True,
            error=error,
            saved=False,
        )

    _atomic_write(path, content)
    return render_template(
        "partials/compliance_exception_editor.html",
        section=section,
        filename=filename,
        content=content,
        can_write=True,
        error=None,
        saved=True,
    )


@bp.post("/compliance-exceptions/<section>/create")
def create(section: str) -> str | Response:
    if not can_write():
        return Response("Forbidden — rw or admin role required.", status=403)
    if section not in SECTIONS:
        return Response("Not found.", status=404)

    filename, error = normalise_new_filename(section, request.form.get("filename", ""))
    if error is not None:
        return _render_section(section, error=error)

    path = safe_path(section, filename)
    if path is None:  # defensive — normalise_new_filename already validated it
        return _render_section(section, error="Invalid filename.")
    if path.exists():
        return _render_section(section, error=f"'{filename}' already exists.")

    _atomic_write(path, "")
    return _render_section(section, created=filename)


@bp.post("/compliance-exceptions/<section>/<filename>/delete")
def delete(section: str, filename: str) -> str | Response:
    if not can_write():
        return Response("Forbidden — rw or admin role required.", status=403)

    path = safe_path(section, filename)
    if path is None or not path.exists():
        return Response("Not found.", status=404)
    if is_protected(section, filename):
        return _render_section(section, error=f"'{filename}' is required and cannot be deleted.")

    path.unlink()
    return _render_section(section, deleted=filename)
