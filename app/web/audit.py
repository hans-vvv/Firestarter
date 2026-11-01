"""GUI transaction (audit) logging for the dashboard.

Two layers, both writing structured ``key=value`` lines to the dedicated
``audit`` logger (``transactions.log`` — see :mod:`app.logging.logger`):

* **Automatic** — an ``after_request`` hook emits one line for every *mutating*
  request (POST/PUT/PATCH/DELETE), capturing who (user + role), from where (ip),
  what (method, endpoint, path), the outcome (HTTP status) and duration. This is
  the completeness guarantee: no mutating route can silently escape the audit
  trail. It logs only request *metadata* — never form bodies — so passwords and
  device secrets are never written.

* **Semantic** — routes call :func:`set_audit_context` to enrich their request
  line with domain fields (``event``, ``target`` device/site, …), or
  :func:`audit_event` to record an outcome the HTTP status alone doesn't convey
  (e.g. a pipeline run that returns 200 but failed internally). These add meaning
  on top of the automatic line; they never carry secret values.
"""

from __future__ import annotations

import time
from typing import Any

from flask import Flask, g, request
from werkzeug.wrappers import Response

from app.logging.logger import get_audit_logger
from app.web.auth import current_user

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_START_ATTR = "_audit_start"
_EXTRA_ATTR = "_audit_extra"


def _client_ip() -> str:
    """Best-effort client IP: first ``X-Forwarded-For`` hop, else the peer.

    Operators reach the dashboard from ``10.91.1.0/24``; if a reverse proxy is
    ever put in front, the real client is the left-most forwarded address.
    """
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "-"


def _identity() -> tuple[str, str]:
    """Return ``(username, role)`` for the current session, or anonymous."""
    user = current_user()
    if not user:
        return "anonymous", "-"
    return user.get("username", "-"), user.get("role", "-")


def _format(fields: dict[str, Any]) -> str:
    """Render an ordered mapping as ``key=value``, quoting awkward values.

    ``None`` values are dropped. Values containing whitespace, ``=`` or quotes
    are wrapped in double quotes with inner quotes escaped, so each record stays
    a single parseable line.
    """
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value)
        if text == "" or any(c in text for c in ' \t"='):
            text = '"' + text.replace('"', '\\"') + '"'
        parts.append(f"{key}={text}")
    return " ".join(parts)


def set_audit_context(**fields: Any) -> None:
    """Attach domain fields (event, target, …) to this request's audit line.

    Merged into the automatic ``after_request`` record, so a route adds meaning
    with a single call and no second log line. Never pass secret values.
    """
    extra: dict[str, Any] = getattr(g, _EXTRA_ATTR, None) or {}
    extra.update(fields)
    setattr(g, _EXTRA_ATTR, extra)


def audit_event(event: str, **fields: Any) -> None:
    """Emit a standalone audit record for a domain outcome.

    Use when the HTTP status doesn't capture the result (partial success, an
    internally-caught failure) or when there is no mutating request to hang the
    line on. Prefixed with the current identity and source IP. Never pass secret
    values.
    """
    username, role = _identity()
    line = _format(
        {
            "user": username,
            "role": role,
            "ip": _client_ip(),
            "event": event,
            **fields,
        }
    )
    get_audit_logger().info(line)


def register_audit(app: Flask) -> None:
    """Wire the automatic request-audit hooks onto *app*."""

    @app.before_request
    def _mark_start() -> None:
        setattr(g, _START_ATTR, time.perf_counter())

    @app.after_request
    def _record(response: Response) -> Response:
        if request.method not in _MUTATING_METHODS or request.endpoint == "static":
            return response

        start = getattr(g, _START_ATTR, None)
        dur_ms = round((time.perf_counter() - start) * 1000) if start is not None else None

        username, role = _identity()
        fields: dict[str, Any] = {
            "user": username,
            "role": role,
            "ip": _client_ip(),
            "method": request.method,
            "endpoint": request.endpoint or "-",
            "path": request.path,
            "status": response.status_code,
            "dur_ms": dur_ms,
        }
        # Domain enrichment (event/target/…) goes last so it reads naturally.
        fields.update(getattr(g, _EXTRA_ATTR, None) or {})
        get_audit_logger().info(_format(fields))
        return response
