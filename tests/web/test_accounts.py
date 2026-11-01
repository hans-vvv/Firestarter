from __future__ import annotations

"""Unit tests for app.web.accounts — auth/account domain logic against a DB session."""

import pytest

from app.repositories.user import count_users, get_user_by_username
from app.web import accounts

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


class TestHashing:
    def test_hash_is_not_plaintext_and_verifies(self):
        h = accounts.hash_password("s3cret-pw")
        assert h != "s3cret-pw"
        assert accounts.verify_password(h, "s3cret-pw")
        assert not accounts.verify_password(h, "wrong")

    def test_hashes_are_salted(self):
        assert accounts.hash_password("x") != accounts.hash_password("x")


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


class TestSeeding:
    def test_seed_roles_idempotent(self, session):
        accounts.seed_user_roles(session)
        accounts.seed_user_roles(session)
        from app.repositories.user import get_all_user_roles

        names = sorted(r.name for r in get_all_user_roles(session))
        assert names == ["admin", "ro", "rw"]

    def test_seed_default_admin_creates_admin_once(self, session, monkeypatch):
        monkeypatch.delenv(accounts.DEMO_ENV, raising=False)
        monkeypatch.delenv(accounts.ADMIN_PASSWORD_ENV, raising=False)
        accounts.seed_default_admin(session)
        admin = get_user_by_username(session, "admin")
        assert admin is not None
        assert admin.role.name == "admin"
        assert admin.must_change_password is True
        assert accounts.verify_password(admin.password_hash, "changeme")

        # Idempotent: a second call does not add another user.
        accounts.seed_default_admin(session)
        assert count_users(session) == 1

    def test_bootstrap_password_comes_from_the_environment(self, session, monkeypatch):
        monkeypatch.delenv(accounts.DEMO_ENV, raising=False)
        monkeypatch.setenv(accounts.ADMIN_PASSWORD_ENV, "demo-pass-42")
        accounts.seed_default_admin(session)
        admin = get_user_by_username(session, "admin")
        assert admin is not None
        assert accounts.verify_password(admin.password_hash, "demo-pass-42")
        assert not accounts.verify_password(admin.password_hash, "changeme")
        # Outside demo mode the first login still forces a change.
        assert admin.must_change_password is True

    def test_empty_env_password_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(accounts.ADMIN_PASSWORD_ENV, "")
        assert accounts.bootstrap_admin_password() == accounts.DEFAULT_ADMIN_PASSWORD

    @pytest.mark.parametrize("flag", ["1", "true", "YES", "on"])
    def test_demo_mode_does_not_force_a_password_change(self, session, monkeypatch, flag):
        monkeypatch.setenv(accounts.DEMO_ENV, flag)
        monkeypatch.delenv(accounts.ADMIN_PASSWORD_ENV, raising=False)
        accounts.seed_default_admin(session)
        admin = get_user_by_username(session, "admin")
        assert admin is not None
        assert admin.must_change_password is False
        assert accounts.verify_password(admin.password_hash, "changeme")

    @pytest.mark.parametrize("flag", ["", "0", "false", "no"])
    def test_demo_mode_is_off_unless_truthy(self, monkeypatch, flag):
        monkeypatch.setenv(accounts.DEMO_ENV, flag)
        assert accounts.is_demo_mode() is False

    def test_seed_default_admin_noop_when_users_exist(self, session):
        accounts.seed_user_roles(session)
        accounts.create_user(session, username="alice", role_name="rw", password="pw")
        accounts.seed_default_admin(session)
        assert get_user_by_username(session, "admin") is None


# ---------------------------------------------------------------------------
# create_user
# ---------------------------------------------------------------------------


class TestCreateUser:
    def test_creates_with_must_change_flag(self, session):
        accounts.seed_user_roles(session)
        u = accounts.create_user(session, username="alice", role_name="ro", password="tmp12345")
        assert u.username == "alice"
        assert u.role.name == "ro"
        assert u.must_change_password is True
        assert accounts.verify_password(u.password_hash, "tmp12345")

    def test_duplicate_username_rejected(self, session):
        accounts.seed_user_roles(session)
        accounts.create_user(session, username="alice", role_name="ro", password="pw")
        with pytest.raises(ValueError, match="already exists"):
            accounts.create_user(session, username="alice", role_name="rw", password="pw")

    def test_unknown_role_rejected(self, session):
        accounts.seed_user_roles(session)
        with pytest.raises(ValueError, match="Unknown role"):
            accounts.create_user(session, username="bob", role_name="superuser", password="pw")

    @pytest.mark.parametrize(("username", "password"), [("", "pw"), ("  ", "pw"), ("bob", "")])
    def test_empty_fields_rejected(self, session, username, password):
        accounts.seed_user_roles(session)
        with pytest.raises(ValueError, match="must not be empty"):
            accounts.create_user(session, username=username, role_name="ro", password=password)


# ---------------------------------------------------------------------------
# authenticate
# ---------------------------------------------------------------------------


class TestAuthenticate:
    def test_valid_credentials_stamp_last_login(self, session):
        accounts.seed_user_roles(session)
        accounts.create_user(session, username="alice", role_name="ro", password="pw123456")
        user = accounts.authenticate(session, username="alice", password="pw123456")
        assert user is not None
        assert user.last_login_at is not None

    def test_wrong_password_returns_none(self, session):
        accounts.seed_user_roles(session)
        accounts.create_user(session, username="alice", role_name="ro", password="pw123456")
        assert accounts.authenticate(session, username="alice", password="nope") is None

    def test_unknown_user_returns_none(self, session):
        assert accounts.authenticate(session, username="ghost", password="x") is None


# ---------------------------------------------------------------------------
# password change / reset
# ---------------------------------------------------------------------------


class TestPasswordLifecycle:
    def test_change_password_clears_flag(self, session):
        accounts.seed_user_roles(session)
        u = accounts.create_user(session, username="alice", role_name="ro", password="old")
        accounts.change_password(session, user_id=u.id, new_password="brandnewpw")
        assert u.must_change_password is False
        assert accounts.verify_password(u.password_hash, "brandnewpw")

    def test_reset_password_sets_flag(self, session):
        accounts.seed_user_roles(session)
        u = accounts.create_user(session, username="alice", role_name="ro", password="old")
        accounts.change_password(session, user_id=u.id, new_password="settled")
        accounts.reset_password(session, user_id=u.id, temp_password="temp9999")
        assert u.must_change_password is True
        assert accounts.verify_password(u.password_hash, "temp9999")


# ---------------------------------------------------------------------------
# set_role + delete safety rails
# ---------------------------------------------------------------------------


class TestSafetyRails:
    def test_cannot_demote_last_admin(self, session):
        accounts.seed_default_admin(session)
        admin = get_user_by_username(session, "admin")
        with pytest.raises(ValueError, match="last administrator"):
            accounts.set_role(session, user_id=admin.id, role_name="ro")

    def test_can_demote_admin_when_another_admin_exists(self, session):
        accounts.seed_default_admin(session)
        admin = get_user_by_username(session, "admin")
        accounts.create_user(session, username="admin2", role_name="admin", password="pw")
        accounts.set_role(session, user_id=admin.id, role_name="ro")
        assert admin.role.name == "ro"

    def test_cannot_delete_self(self, session):
        accounts.seed_default_admin(session)
        admin = get_user_by_username(session, "admin")
        with pytest.raises(ValueError, match="your own account"):
            accounts.delete_user(session, user_id=admin.id, acting_user_id=admin.id)

    def test_cannot_delete_last_admin(self, session):
        accounts.seed_default_admin(session)
        admin = get_user_by_username(session, "admin")
        accounts.create_user(session, username="other", role_name="rw", password="pw")
        other = get_user_by_username(session, "other")
        with pytest.raises(ValueError, match="last administrator"):
            accounts.delete_user(session, user_id=admin.id, acting_user_id=other.id)

    def test_delete_user_removes_row(self, session):
        accounts.seed_default_admin(session)
        admin = get_user_by_username(session, "admin")
        accounts.create_user(session, username="victim", role_name="rw", password="pw")
        victim = get_user_by_username(session, "victim")
        accounts.delete_user(session, user_id=victim.id, acting_user_id=admin.id)
        assert get_user_by_username(session, "victim") is None
