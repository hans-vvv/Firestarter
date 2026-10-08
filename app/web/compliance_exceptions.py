"""Helpers for managing compliance-exception files from the dashboard.

The compliance normaliser reads two directories of operator-maintained files:

    app/compliance/extra/   — ``*.cfg`` plain config lines injected into the
                              rendered side (named ``<role>.cfg`` / ``<hostname>.cfg``).
    app/compliance/ignore/  — ``*.yaml`` ignore + redact rule files
                              (named ``base.yaml`` / ``<model>.yaml`` /
                              ``<role>.yaml`` / ``<hostname>.yaml``).

This module is the pure (no-Flask) layer behind the ``compliance_exceptions``
blueprint: directory listing, path-safety, filename normalisation, and
content validation.  Keeping it free of request handling makes the safety
rules — which are the whole point — directly unit-testable.

Path safety is enforced in one place (:func:`safe_path`): a candidate
filename must be a single path component drawn from a strict character set,
must carry the section's required extension, and must resolve to a child of
the section directory.  No user input is ever concatenated onto a path
without passing through it.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.compliance.normaliser import _compile_redact_rules, _compile_rules
from app.domain.file_locations import COMPLIANCE_EXTRA_LOC, COMPLIANCE_IGNORE_LOC

# Resolved under the live data root (repo root by default, FIRESTARTER_DATA when
# set) so the directories are correct whatever directory the Flask process
# started from.  Module-level so tests can monkeypatch them at a single point.
EXTRA_DIR = COMPLIANCE_EXTRA_LOC.path
IGNORE_DIR = COMPLIANCE_IGNORE_LOC.path

# Static per-section metadata. The directory is deliberately NOT baked in here
# (it is read live via :func:`section_dir`) so monkeypatching EXTRA_DIR /
# IGNORE_DIR in tests takes effect without rebuilding this table.
SECTIONS: dict[str, dict] = {
    "extra": {
        "ext": ".cfg",
        "label": "Extra config",
        "description": "Lines that must be present on the device but the renderer "
        "does not yet model. Injected into the rendered side. base.cfg applies to "
        "every device; role/host files layer on top.",
        "icon": "bi-file-earmark-plus",
        "placeholder": "base.cfg, role.cfg or hostname.cfg",
        # No file in extra/ is load-critical (base.cfg included — it is optional,
        # unlike ignore/base.yaml), so none are protected.
        "protected": frozenset(),
    },
    "ignore": {
        "ext": ".yaml",
        "label": "Ignore & redact rules",
        "description": "Lines to drop from the diff (platform defaults) or redact "
        "(secrets). base.yaml applies to every device; the rest layer on top.",
        "icon": "bi-funnel",
        "placeholder": "hostname.yaml or role.yaml",
        # base.yaml is required by the normaliser — deleting it breaks every run.
        "protected": frozenset({"base.yaml"}),
    },
}

# A safe filename is a single path component: letters, digits, dot, dash,
# underscore only. This blocks both directory separators and absolute paths,
# so the resolved path can never escape the section directory.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def section_dir(section_key: str) -> Path | None:
    """Return the on-disk directory for *section_key*, or None if unknown.

    Reads the module globals at call time (not a cached table) so tests can
    repoint EXTRA_DIR / IGNORE_DIR.
    """
    return {"extra": EXTRA_DIR, "ignore": IGNORE_DIR}.get(section_key)


def is_protected(section_key: str, filename: str) -> bool:
    """Return True if *filename* must not be deleted (case-insensitive)."""
    sec = SECTIONS.get(section_key)
    if sec is None:
        return False
    return filename.lower() in sec["protected"]


def list_files(section_key: str) -> list[str]:
    """Return the sorted exception filenames in *section_key*'s directory.

    Only files carrying the section's extension are returned, so the ``ignore``
    directory's ``__init__.py`` package marker is naturally excluded.
    """
    sec = SECTIONS.get(section_key)
    directory = section_dir(section_key)
    if sec is None or directory is None or not directory.exists():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_file() and p.suffix == sec["ext"])


def safe_path(section_key: str, filename: str) -> Path | None:
    """Return the absolute path for *filename* in *section_key*, or None.

    None is returned for an unknown section, a filename outside the allowed
    character set, a filename missing the required extension, or any path that
    would resolve outside the section directory. This is the single chokepoint
    for path safety — callers never build paths themselves.
    """
    sec = SECTIONS.get(section_key)
    directory = section_dir(section_key)
    if sec is None or directory is None:
        return None
    if not _SAFE_NAME.match(filename):
        return None
    if not filename.endswith(sec["ext"]):
        return None
    candidate = directory / filename
    # Defensive: even with the character-set guard, confirm the resolved file
    # sits directly inside the section directory before handing the path back.
    if candidate.resolve().parent != directory.resolve():
        return None
    return candidate


def normalise_new_filename(section_key: str, raw: str) -> tuple[str | None, str | None]:
    """Turn raw create-form input into a validated filename.

    Returns ``(filename, None)`` on success or ``(None, error)`` on failure.
    The section extension is appended when the operator omits it, so typing
    ``pe1.tst-001`` in the ignore section yields ``pe1.tst-001.yaml``.
    """
    sec = SECTIONS.get(section_key)
    if sec is None:
        return None, "Unknown section."

    name = raw.strip()
    if not name:
        return None, "Filename is required."

    if not name.endswith(sec["ext"]):
        name = name + sec["ext"]

    # Validate the final name through the same chokepoint used for reads/writes.
    if safe_path(section_key, name) is None:
        return None, (
            "Invalid filename. Use letters, digits, dot, dash and underscore "
            f"only (e.g. {sec['placeholder']})."
        )
    return name, None


def validate_content(section_key: str, content: str) -> str | None:
    """Return an error string if *content* is invalid for the section, else None.

    ``extra`` files are plain config text with no structure, so anything is
    accepted. ``ignore`` files are validated by actually compiling them through
    the normaliser's rule compilers — the exact code path a compliance run
    uses — so a file that would crash a run is rejected here instead.
    """
    if section_key == "extra":
        return None
    if section_key == "ignore":
        return _validate_ignore_yaml(content)
    return "Unknown section."


def _validate_ignore_yaml(content: str) -> str | None:
    """Validate an ignore/redact YAML document, mirroring the normaliser's load.

    An empty document is valid (no rules). The top level must be a mapping with
    optional ``ignore`` / ``redact`` lists, and every rule must compile — an
    unknown ``match`` type or a redact rule missing ``replace`` is rejected with
    the compiler's own message.
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return f"YAML syntax error: {exc}"

    if data is None:
        return None
    if not isinstance(data, dict):
        return "Top-level YAML must be a mapping with 'ignore' and/or 'redact' keys."

    raw_ignore = data.get("ignore", []) or []
    raw_redact = data.get("redact", []) or []
    if not isinstance(raw_ignore, list) or not isinstance(raw_redact, list):
        return "'ignore' and 'redact' must be lists of rule mappings."

    try:
        _compile_rules(raw_ignore)
        _compile_redact_rules(raw_redact)
    except KeyError as exc:
        return f"Rule is missing required key: {exc}."
    except (re.error, ValueError, TypeError, AttributeError) as exc:
        return f"Invalid rule: {exc}"
    return None
