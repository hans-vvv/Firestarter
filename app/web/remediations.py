"""Helpers for managing compliance-remediation spec files from the dashboard.

The remediation engine (:mod:`app.compliance.remediation.engine`) reads one
directory of operator-maintained files:

    app/compliance/remediation/   — ``<role>.yaml`` per-role remediation specs
                                    (``add`` rules, ``delete`` rules with
                                    ``remediation_commands``, and optional ``conditions``).
                                    See ADR 0004.

This module is the pure (no-Flask) layer behind the ``remediations`` blueprint:
directory listing, path-safety, filename normalisation, atomic writes, and
content validation. Keeping it free of request handling makes the safety rules —
which are the whole point — directly unit-testable.

Spec files are keyed by ``Role.name``, **not** hostname: the ``core1.*`` devices
have role ``core``, so their file is ``core.yaml``, not ``core1.yaml``. The create-form
placeholder says so, to defuse a mistake that has bitten before.

Path safety is enforced in one place (:func:`safe_path`): a candidate filename
must be a single path component drawn from a strict character set, must carry the
``.yaml`` extension, and must resolve to a child of the remediation directory. No
user input is ever concatenated onto a path without passing through it. The
tracked package files (``__init__.py``, ``README.md``, ``engine.py``) are not
``.yaml`` and so can never be targeted or listed.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

import yaml

# The allow grammar IS the compliance ignore grammar — validate a spec by
# compiling it through the exact code path a remediation run uses, so a file that
# would crash a run is rejected here instead of on the next run.
from app.compliance.normaliser import _compile_rules
from app.compliance.remediation import command_placeholders
from app.domain.file_locations import COMPLIANCE_REMEDIATION_LOC

# Resolved under the live data root (repo root by default, FIRESTARTER_DATA when
# set) so the directory is correct whatever directory the Flask process started
# from.  Module-level so tests can monkeypatch it at a single point.
REMEDIATION_DIR = COMPLIANCE_REMEDIATION_LOC.path

EXT = ".yaml"

# A safe filename is a single path component: letters, digits, dot, dash,
# underscore only. This blocks both directory separators and absolute paths, so
# the resolved path can never escape the remediation directory.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

PLACEHOLDER = "pe.yaml (role name — not hostname)"


def _dir(remediation_dir: Path | None) -> Path:
    """Resolve the directory to use, reading ``REMEDIATION_DIR`` live when unset.

    Defaulting to ``None`` and dereferencing the module global here (rather than in
    a default argument, which binds once at definition time) is what lets tests
    monkeypatch ``REMEDIATION_DIR`` at a single point and have every helper follow.
    """
    return remediation_dir if remediation_dir is not None else REMEDIATION_DIR


def list_files(*, remediation_dir: Path | None = None) -> list[str]:
    """Return the sorted spec filenames in the remediation directory.

    Only ``.yaml`` files are returned, so the package's tracked ``__init__.py``,
    ``README.md`` and ``engine.py`` are naturally excluded.
    """
    directory = _dir(remediation_dir)
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_file() and p.suffix == EXT)


def safe_path(filename: str, *, remediation_dir: Path | None = None) -> Path | None:
    """Return the absolute path for *filename*, or ``None`` if it is not safe.

    ``None`` is returned for a filename outside the allowed character set, one
    missing the ``.yaml`` extension, or any path that would resolve outside the
    remediation directory. This is the single chokepoint for path safety —
    callers never build paths themselves.
    """
    directory = _dir(remediation_dir)
    if not _SAFE_NAME.match(filename):
        return None
    if not filename.endswith(EXT):
        return None
    candidate = directory / filename
    # Defensive: even with the character-set guard, confirm the resolved file
    # sits directly inside the remediation directory before handing the path back.
    if candidate.resolve().parent != directory.resolve():
        return None
    return candidate


def normalise_new_filename(raw: str) -> tuple[str | None, str | None]:
    """Turn raw create-form input into a validated filename.

    Returns ``(filename, None)`` on success or ``(None, error)`` on failure. The
    ``.yaml`` extension is appended when omitted, so typing ``pe`` yields
    ``pe.yaml``.
    """
    name = raw.strip()
    if not name:
        return None, "Filename is required."

    if not name.endswith(EXT):
        name = name + EXT

    if safe_path(name) is None:
        return None, (
            "Invalid filename. Use letters, digits, dot, dash and underscore "
            f"only (e.g. {PLACEHOLDER})."
        )
    return name, None


def validate_content(content: str) -> str | None:
    """Return an error string if *content* is not a valid remediation spec, else None.

    Mirrors :func:`app.compliance.remediation.engine.load_remediation_spec`: an empty
    document is valid (no rules) and the top level must be a mapping. Two rule keys are
    validated against the real matcher grammar:

    - ``add`` — a list of rule mappings that compile; the additive lines are *derived*
      from the match, so an ``add`` rule may **not** carry ``remediation_commands``.
    - ``delete`` — a list of rule mappings that compile; a delete command cannot be
      derived from the match, so each ``delete`` rule **must** carry a non-empty
      ``remediation_commands`` list of strings (the explicit removal commands). A command
      may reference the rule's regex capture groups with ``{name}`` placeholders; every
      placeholder must correspond to a named group the rule's ``regex`` value defines,
      so a typo (or a placeholder on an ``exact`` / ``startswith`` rule, which captures
      nothing) is rejected here rather than pushing a literal ``{name}`` to a device.

    The former ``allow`` key was renamed to ``add``; a spec that still uses it is
    rejected with that hint rather than silently ignored (deny-by-default would hide
    the mistake). Any ``conditions`` block must have a string ``status`` and a
    ``labels`` mapping.
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return f"YAML syntax error: {exc}"

    if data is None:
        return None
    if not isinstance(data, Mapping):
        return "Top-level YAML must be a mapping with 'add', 'delete' and/or 'conditions' keys."

    if "allow" in data:
        return "'allow' was renamed to 'add'. Rename the key (deletes now go under 'delete')."

    raw_add = data.get("add", []) or []
    if not isinstance(raw_add, list):
        return "'add' must be a list of rule mappings."
    for i, rule in enumerate(raw_add, start=1):
        if not isinstance(rule, Mapping):
            return f"add rule {i} must be a mapping (with 'match' and 'value')."
        if rule.get("remediation_commands") is not None:
            return (
                f"add rule {i}: 'remediation_commands' is not allowed on 'add' — the added lines "
                "come from the match. Put removal commands under a 'delete' rule."
            )

    raw_delete = data.get("delete", []) or []
    if not isinstance(raw_delete, list):
        return "'delete' must be a list of rule mappings."
    for i, rule in enumerate(raw_delete, start=1):
        if not isinstance(rule, Mapping):
            return (
                f"delete rule {i} must be a mapping "
                "(with 'match', 'value' and 'remediation_commands')."
            )
        extra = rule.get("remediation_commands")
        if not isinstance(extra, list) or not extra or not all(isinstance(x, str) for x in extra):
            return (
                f"delete rule {i}: 'remediation_commands' must be a non-empty list of strings — "
                "the explicit MD-CLI command(s) to run when the rule matches."
            )

        # Cross-check ``{name}`` placeholders against the groups the rule can actually
        # supply. Only a ``regex`` value defines named groups; ``exact`` / ``startswith``
        # capture nothing, so any placeholder on them is unfillable. A malformed regex is
        # left for the ``_compile_rules`` pass below to report, so guard the compile here.
        used = set().union(*(command_placeholders(cmd) for cmd in extra))
        if used:
            named_groups: set[str] = set()
            if rule.get("match") == "regex":
                try:
                    named_groups = set(re.compile(rule["value"]).groupindex)
                except (re.error, KeyError, TypeError):
                    named_groups = used  # defer to the compile-time error below
            unknown = sorted(used - named_groups)
            if unknown:
                return (
                    f"delete rule {i}: remediation command placeholder(s) "
                    f"{', '.join('{' + n + '}' for n in unknown)} have no matching named "
                    "capture group in the rule's regex 'value'. Add e.g. "
                    "'(?P<name>...)' to the pattern, or remove the placeholder."
                )

    try:
        _compile_rules(raw_add)
    except KeyError as exc:
        return f"add rule is missing required key: {exc}."
    except (re.error, ValueError, TypeError, AttributeError) as exc:
        return f"Invalid add rule: {exc}"

    try:
        _compile_rules(raw_delete)
    except KeyError as exc:
        return f"delete rule is missing required key: {exc}."
    except (re.error, ValueError, TypeError, AttributeError) as exc:
        return f"Invalid delete rule: {exc}"

    conditions = data.get("conditions")
    if conditions is not None:
        if not isinstance(conditions, Mapping):
            return "'conditions' must be a mapping."
        status = conditions.get("status")
        if status is not None and not isinstance(status, str):
            return "'conditions.status' must be a string."
        labels = conditions.get("labels")
        if labels is not None and not isinstance(labels, Mapping):
            return "'conditions.labels' must be a mapping of key/value."

    return None


def atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically (temp file + os.replace).

    A half-written spec would feed the next remediation run a corrupt rule set, so
    the new content is staged in a temp file on the same directory and swapped in
    with a single atomic rename.

    Line endings are normalised to ``\\n``: browsers submit textarea content with
    ``\\r\\n`` but the repo stores these files with ``\\n``, and writing the raw
    body back would flip every line in the git diff. ``newline=""`` disables
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
