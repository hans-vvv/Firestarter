"""Tests for the GUI transaction (audit) logging layer, :mod:`app.web.audit`."""

from __future__ import annotations

import logging

import pytest
from flask import Flask, session

from app.web.audit import _format, audit_event, register_audit, set_audit_context


@pytest.fixture
def captured_audit():
    """Capture messages emitted on the ``audit`` logger during a test."""
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _Capture()
    logger = logging.getLogger("audit")
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


@pytest.fixture
def audit_app():
    """A minimal app with the audit hooks and a couple of routes."""
    app = Flask(__name__)
    app.secret_key = "test"
    register_audit(app)

    @app.get("/read")
    def read() -> str:
        return "ok"

    @app.post("/do")
    def do() -> str:
        set_audit_context(event="thing.do", target="dev1")
        return "ok"

    @app.post("/whoami")
    def whoami() -> str:
        return "ok"

    return app


# ---------------------------------------------------------------------------
# _format
# ---------------------------------------------------------------------------


def test_format_quotes_awkward_values_and_drops_none():
    line = _format({"user": "bob", "note": "a b", "empty": "", "skip": None, "n": 3})
    assert line == 'user=bob note="a b" empty="" n=3'


def test_format_escapes_inner_quotes():
    assert _format({"msg": 'he said "hi"'}) == 'msg="he said \\"hi\\""'


# ---------------------------------------------------------------------------
# after_request automatic layer
# ---------------------------------------------------------------------------


def test_mutating_request_is_logged_once_with_metadata(audit_app, captured_audit):
    audit_app.test_client().post("/do")
    assert len(captured_audit) == 1
    line = captured_audit[0]
    assert "method=POST" in line
    assert "endpoint=do" in line
    assert "status=200" in line
    assert "user=anonymous" in line
    assert "ip=" in line
    assert "dur_ms=" in line
    # Domain enrichment from set_audit_context is merged into the same line.
    assert "event=thing.do" in line
    assert "target=dev1" in line


def test_get_requests_are_not_logged(audit_app, captured_audit):
    audit_app.test_client().get("/read")
    assert captured_audit == []


def test_identity_taken_from_session(audit_app, captured_audit):
    client = audit_app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"id": 5, "username": "alice", "role": "rw"}
    client.post("/whoami")
    assert len(captured_audit) == 1
    assert "user=alice" in captured_audit[0]
    assert "role=rw" in captured_audit[0]


def test_x_forwarded_for_first_hop_is_used(audit_app, captured_audit):
    audit_app.test_client().post("/do", headers={"X-Forwarded-For": "10.91.1.7, 10.0.0.1"})
    assert "ip=10.91.1.7" in captured_audit[0]


def test_form_body_is_never_logged(audit_app, captured_audit):
    audit_app.test_client().post("/do", data={"password": "sup3rsecret"})
    line = captured_audit[0]
    assert "sup3rsecret" not in line
    assert "password" not in line


# ---------------------------------------------------------------------------
# audit_event standalone layer
# ---------------------------------------------------------------------------


def test_audit_event_emits_identity_and_fields(audit_app, captured_audit):
    with audit_app.test_request_context("/x", method="POST"):
        session["user"] = {"id": 1, "username": "carol", "role": "admin"}
        audit_event("auth.logout", extra="z")
    assert len(captured_audit) == 1
    line = captured_audit[0]
    assert "event=auth.logout" in line
    assert "user=carol" in line
    assert "role=admin" in line
    assert "extra=z" in line


# ---------------------------------------------------------------------------
# End-to-end: create_app wires the audit hooks
# ---------------------------------------------------------------------------


def test_real_app_logs_logout_with_identity(client, captured_audit):
    """The production factory wires register_audit, and auth.logout records the
    acting user before the session is cleared."""
    client.post("/logout")
    joined = "\n".join(captured_audit)
    assert "event=auth.logout" in joined
    assert "user=admin" in joined  # captured before logout cleared the session
