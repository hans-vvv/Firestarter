"""Flask blueprint for viewing and editing service-definition YAML files.

The index page (GET /services) is built dynamically from whatever definition
files exist on disk — no hardcoded mapping.  On a production server only
production files are present; on a lab server only lab files.  The page
adapts automatically.

All mutating routes (POST /services/<path:slug>/edit) require the ``rw`` or
``admin`` role.  Read-only users can view but not save.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml
from flask import Blueprint, Response, render_template, request
from pydantic import ValidationError

from app.validation.service_definition import ServiceDefinitionDocument
from app.web.auth import can_write
from app.web.service_catalogue import CATEGORIES, build_catalogue, resolve_slug

bp = Blueprint("services", __name__)


def _not_found(slug: str) -> Response:
    """Inline 'not found' partial for the htmx editor panel.

    Returning an empty 404 body would silently blank the panel; this renders a
    small dismissible message in its place instead.
    """
    html = render_template("partials/service_not_found.html", slug=slug)
    return Response(html, status=404)


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically (temp file + os.replace).

    A half-written service definition would break the config-generation
    pipeline, so the new content is staged in a temp file on the same
    directory and swapped in with a single atomic rename.

    Line endings are normalised to ``\\n``: browsers submit textarea content
    with ``\\r\\n``, but the repo stores these YAML files with ``\\n``, and
    writing the raw body back would flip every line in the git diff. ``newline=""``
    disables further translation so exactly the normalised bytes are written.
    """
    normalised = content.replace("\r\n", "\n").replace("\r", "\n")
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(normalised)
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise


@bp.get("/services")
def index() -> str:
    cat = build_catalogue()
    return render_template(
        "services.html",
        cat=cat,
        categories=CATEGORIES,
        can_write=can_write(),
    )


@bp.get("/services/<path:slug>/edit")
def editor(slug: str) -> str | Response:
    path = resolve_slug(slug)
    if path is None:
        return _not_found(slug)
    content = path.read_text(encoding="utf-8")
    return render_template(
        "partials/service_editor.html",
        slug=slug,
        filename=path.name,
        content=content,
        can_write=can_write(),
        error=None,
        saved=False,
    )


@bp.post("/services/<path:slug>/edit")
def save(slug: str) -> str | Response:
    if not can_write():
        return Response("Forbidden — rw or admin role required.", status=403)

    path = resolve_slug(slug)
    if path is None:
        return _not_found(slug)

    content = request.form.get("content", "")

    # Validate YAML syntax first, then schema
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return render_template(
            "partials/service_editor.html",
            slug=slug,
            filename=path.name,
            content=content,
            can_write=True,
            error=f"YAML syntax error: {exc}",
            saved=False,
        )

    try:
        ServiceDefinitionDocument.model_validate(doc)
    except ValidationError as exc:
        errors = "; ".join(
            f"{' → '.join(str(l) for l in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        return render_template(
            "partials/service_editor.html",
            slug=slug,
            filename=path.name,
            content=content,
            can_write=True,
            error=f"Validation error: {errors}",
            saved=False,
        )

    _atomic_write(path, content)
    return render_template(
        "partials/service_editor.html",
        slug=slug,
        filename=path.name,
        content=content,
        can_write=True,
        error=None,
        saved=True,
    )
