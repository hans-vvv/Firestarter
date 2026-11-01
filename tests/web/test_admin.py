from __future__ import annotations

"""Route tests for the admin (user-management) blueprint.

Facade calls are monkeypatched; tests cover access control, the htmx partial
responses, and that route actions forward the right arguments to the facade.
"""

import pytest

_USERS = [
    {
        "id": 1,
        "username": "admin",
        "role": "admin",
        "must_change_password": False,
        "created_at": None,
        "last_login_at": None,
    },
    {
        "id": 2,
        "username": "bob",
        "role": "ro",
        "must_change_password": True,
        "created_at": None,
        "last_login_at": None,
    },
]


@pytest.fixture(autouse=True)
def device_password_state(monkeypatch):
    """Keep the device-password card off the real database.

    The admin page reports whether the production device password has been
    recorded, which is a query. The suite has to pass on a checkout carrying no
    environment data (ADR 0001), so it is stubbed here. Autouse so a new test
    cannot silently reacquire the dependency.
    """
    monkeypatch.setattr("app.web.routes.admin.device_admin_password_is_set", lambda: True)


@pytest.fixture
def patched(monkeypatch):
    """Patch the read facade used to render the admin table."""
    monkeypatch.setattr("app.web.routes.admin.list_users", lambda: _USERS)
    monkeypatch.setattr("app.web.routes.admin.list_user_roles", lambda: ["admin", "ro", "rw"])


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


class TestAdminAccess:
    def test_anonymous_redirected(self, anon_client):
        resp = anon_client.get("/admin")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_non_admin_forbidden(self, login_as):
        resp = login_as(role="ro").get("/admin")
        assert resp.status_code == 403

    def test_rw_user_also_forbidden(self, login_as):
        resp = login_as(role="rw").get("/admin")
        assert resp.status_code == 403

    def test_admin_sees_roster(self, client, patched):
        resp = client.get("/admin")
        assert resp.status_code == 200
        assert b"bob" in resp.data
        assert b"Create user" in resp.data


# ---------------------------------------------------------------------------
# Mutations return the table partial
# ---------------------------------------------------------------------------


class TestAdminCreate:
    def test_create_forwards_args_and_returns_table(self, client, patched, monkeypatch):
        recorded = {}
        monkeypatch.setattr("app.web.routes.admin.create_user", lambda **kw: recorded.update(kw))
        resp = client.post(
            "/admin/users",
            data={"username": "carol", "role": "rw", "password": "tmp12345"},
        )
        assert resp.status_code == 200
        assert recorded == {"username": "carol", "role_name": "rw", "password": "tmp12345"}
        assert b'id="user-table"' in resp.data

    def test_create_error_is_rendered_inline(self, client, patched, monkeypatch):
        def _boom(**_):
            raise ValueError("User 'carol' already exists.")

        monkeypatch.setattr("app.web.routes.admin.create_user", _boom)
        resp = client.post(
            "/admin/users", data={"username": "carol", "role": "rw", "password": "x"}
        )
        assert resp.status_code == 200
        assert b"already exists" in resp.data
        assert b'id="user-table"' in resp.data


class TestAdminRoleChange:
    def test_change_role_forwards_args(self, client, patched, monkeypatch):
        recorded = {}
        monkeypatch.setattr("app.web.routes.admin.set_user_role", lambda **kw: recorded.update(kw))
        resp = client.post("/admin/users/2/role", data={"role": "rw"})
        assert resp.status_code == 200
        assert recorded == {"user_id": 2, "role_name": "rw"}


class TestAdminResetPassword:
    def test_reset_forwards_args(self, client, patched, monkeypatch):
        recorded = {}
        monkeypatch.setattr(
            "app.web.routes.admin.reset_user_password", lambda **kw: recorded.update(kw)
        )
        resp = client.post("/admin/users/2/reset-password", data={"temp_password": "tmp99999"})
        assert resp.status_code == 200
        assert recorded == {"user_id": 2, "temp_password": "tmp99999"}


class TestAdminDelete:
    def test_delete_passes_acting_user_id(self, client, patched, monkeypatch):
        recorded = {}
        monkeypatch.setattr("app.web.routes.admin.delete_user", lambda **kw: recorded.update(kw))
        resp = client.post("/admin/users/2/delete")
        assert resp.status_code == 200
        # acting_user_id comes from the session identity (admin id=1).
        assert recorded == {"user_id": 2, "acting_user_id": 1}

    def test_delete_error_is_rendered_inline(self, client, patched, monkeypatch):
        def _boom(**_):
            raise ValueError("Cannot delete the last administrator.")

        monkeypatch.setattr("app.web.routes.admin.delete_user", _boom)
        resp = client.post("/admin/users/1/delete")
        assert resp.status_code == 200
        assert b"last administrator" in resp.data


# ---------------------------------------------------------------------------
# Production device password
# ---------------------------------------------------------------------------


class TestDevicePassword:
    """Recording the production admin password used on devices."""

    def test_non_admin_cannot_set_it(self, login_as, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.admin.set_device_admin_password",
            lambda **_: pytest.fail("must not be reached"),
        )
        resp = login_as(role="rw").post(
            "/admin/device-password",
            data={"device_password": "x", "device_password_confirm": "x"},
        )
        assert resp.status_code in (302, 403)

    def test_mismatched_confirmation_is_rejected(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.admin.set_device_admin_password",
            lambda **_: pytest.fail("must not be reached"),
        )

        body = client.post(
            "/admin/device-password",
            data={"device_password": "one", "device_password_confirm": "two"},
        ).data.decode()

        assert "do not match" in body

    def test_empty_password_is_rejected(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.web.routes.admin.set_device_admin_password",
            lambda **_: pytest.fail("must not be reached"),
        )

        body = client.post(
            "/admin/device-password",
            data={"device_password": "", "device_password_confirm": ""},
        ).data.decode()

        assert "Enter a password" in body

    def test_matching_password_is_recorded(self, client, monkeypatch):
        received = {}
        monkeypatch.setattr(
            "app.web.routes.admin.set_device_admin_password",
            lambda *, password: received.update(password=password),
        )

        body = client.post(
            "/admin/device-password",
            data={"device_password": "S3cret!", "device_password_confirm": "S3cret!"},
        ).data.decode()

        assert received == {"password": "S3cret!"}
        assert "Password recorded" in body

    def test_password_is_not_echoed_back(self, client, monkeypatch):
        monkeypatch.setattr("app.web.routes.admin.set_device_admin_password", lambda **_: None)

        body = client.post(
            "/admin/device-password",
            data={"device_password": "S3cret!", "device_password_confirm": "S3cret!"},
        ).data.decode()

        assert "S3cret!" not in body
