from __future__ import annotations

"""Shared fixtures for Flask route tests.

flask_app   — a TESTING-mode Flask app built with create_app(bootstrap=False),
              so importing the app never touches the real database; route tests
              patch facade functions via monkeypatch.
client      — a test client pre-authenticated as an admin (most route tests
              assume an authenticated session now that every page is gated).
anon_client — an unauthenticated client, for testing the login gate itself.
login_as    — factory for a client authenticated as an arbitrary identity.
"""

import pytest

from app.web import create_app

# Identity the authenticated `client` fixture carries. Admin + no forced change
# so it can reach every gated page, including the admin panel.
ADMIN_SESSION = {
    "id": 1,
    "username": "admin",
    "role": "admin",
    "must_change_password": False,
}


@pytest.fixture
def flask_app():
    # bootstrap=False: no schema creation / seeding against the real DB on import.
    app = create_app(bootstrap=False)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def anon_client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def client(flask_app):
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = dict(ADMIN_SESSION)
    return c


@pytest.fixture
def login_as(flask_app):
    """Return a factory that builds a client authenticated as a given identity.

    Usage: ``login_as(role="ro")`` or ``login_as(must_change=True)``.
    """

    def _make(
        *,
        user_id: int = 2,
        username: str = "bob",
        role: str = "ro",
        must_change: bool = False,
    ):
        c = flask_app.test_client()
        with c.session_transaction() as sess:
            sess["user"] = {
                "id": user_id,
                "username": username,
                "role": role,
                "must_change_password": must_change,
            }
        return c

    return _make
