from __future__ import annotations

"""Route tests for the auth blueprint and the global login/forced-change gate.

All DB-touching facade calls are monkeypatched; these tests exercise HTTP flow,
session handling, and redirects only.
"""

ADMIN = {"id": 1, "username": "admin", "role": "admin"}


# ---------------------------------------------------------------------------
# Global gate (before_request)
# ---------------------------------------------------------------------------


class TestLoginGate:
    def test_anonymous_redirected_to_login(self, anon_client):
        resp = anon_client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
        assert "next=/" in resp.headers["Location"]

    def test_login_page_itself_is_open(self, anon_client):
        resp = anon_client.get("/login")
        assert resp.status_code == 200
        assert b"Sign in" in resp.data

    def test_authenticated_user_reaches_pages(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.overview.list_devices", lambda: [])
        monkeypatch.setattr("app.web.routes.overview.list_jobs", lambda: [])
        assert client.get("/").status_code == 200

    def test_must_change_password_funnels_to_change_page(self, login_as):
        c = login_as(must_change=True)
        resp = c.get("/devices")
        assert resp.status_code == 302
        assert "/change-password" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------


class TestLogin:
    def test_success_redirects_and_sets_session(self, anon_client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.auth_routes.authenticate_user",
            lambda **_: {**ADMIN, "must_change_password": False},
        )
        resp = anon_client.post("/login", data={"username": "admin", "password": "pw"})
        assert resp.status_code == 302
        assert resp.headers["Location"] in ("/", "http://localhost/")
        with anon_client.session_transaction() as sess:
            assert sess["user"]["username"] == "admin"

    def test_success_honours_safe_next(self, anon_client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.auth_routes.authenticate_user",
            lambda **_: {**ADMIN, "must_change_password": False},
        )
        resp = anon_client.post(
            "/login", data={"username": "admin", "password": "pw", "next": "/devices"}
        )
        assert resp.headers["Location"].endswith("/devices")

    def test_external_next_is_ignored(self, anon_client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.auth_routes.authenticate_user",
            lambda **_: {**ADMIN, "must_change_password": False},
        )
        resp = anon_client.post(
            "/login", data={"username": "admin", "password": "pw", "next": "//evil.com"}
        )
        assert "evil.com" not in resp.headers["Location"]

    def test_forced_change_redirects_to_change_password(self, anon_client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.auth_routes.authenticate_user",
            lambda **_: {**ADMIN, "must_change_password": True},
        )
        resp = anon_client.post("/login", data={"username": "admin", "password": "changeme"})
        assert resp.status_code == 302
        assert "/change-password" in resp.headers["Location"]

    def test_bad_credentials_return_401(self, anon_client, monkeypatch):
        monkeypatch.setattr("app.web.routes.auth_routes.authenticate_user", lambda **_: None)
        resp = anon_client.post("/login", data={"username": "x", "password": "y"})
        assert resp.status_code == 401
        assert b"Invalid username or password" in resp.data

    def test_logout_clears_session(self, client):
        resp = client.post("/logout")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
        with client.session_transaction() as sess:
            assert "user" not in sess


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------


class TestChangePassword:
    def test_rejects_short_password(self, login_as):
        c = login_as(must_change=True)
        resp = c.post(
            "/change-password", data={"new_password": "short", "confirm_password": "short"}
        )
        assert resp.status_code == 400
        assert b"at least 8" in resp.data

    def test_rejects_mismatch(self, login_as):
        c = login_as(must_change=True)
        resp = c.post(
            "/change-password",
            data={"new_password": "longenough1", "confirm_password": "different11"},
        )
        assert resp.status_code == 400
        assert b"do not match" in resp.data

    def test_success_clears_flag_and_redirects(self, login_as, monkeypatch):
        recorded = {}
        monkeypatch.setattr(
            "app.web.routes.auth_routes.change_user_password",
            lambda **kw: recorded.update(kw),
        )
        c = login_as(user_id=7, must_change=True)
        resp = c.post(
            "/change-password",
            data={"new_password": "longenough1", "confirm_password": "longenough1"},
        )
        assert resp.status_code == 302
        assert recorded == {"user_id": 7, "new_password": "longenough1"}
        with c.session_transaction() as sess:
            assert sess["user"]["must_change_password"] is False
