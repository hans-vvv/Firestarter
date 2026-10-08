"""User-account domain logic for the dashboard auth system.

These functions take an open SQLAlchemy ``Session`` and hold the business rules
(password hashing, the forced-change flag, and the safety rails that stop the
dashboard ever losing its last administrator). Keeping them session-taking and
Flask-free makes them unit-testable with the in-memory ``session`` fixture; the
thin ``db_session`` wrappers in :mod:`app.web.utils` adapt them to HTTP routes.

Bootstrapping (``seed_user_roles`` / ``seed_default_admin``) is idempotent so it
is safe to run on every app start.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from app.models import User, UserRole
from app.repositories.user import (
    count_admins,
    count_users,
    get_user_by_id,
    get_user_by_username,
    get_user_role_by_name,
)
from app.utils import require

# Roles seeded on first boot. "admin" is special-cased by the safety rails; the
# others are stored/displayed only this sprint (enforcement is a later sprint).
DEFAULT_ROLES = ("admin", "ro", "rw")
ADMIN_ROLE = "admin"
DEFAULT_ADMIN_USERNAME = "admin"
# Well-known bootstrap password, used unless FIRESTARTER_ADMIN_PASSWORD is set;
# forced-change on first login outside demo mode (see seed_default_admin).
DEFAULT_ADMIN_PASSWORD = "changeme"
ADMIN_PASSWORD_ENV = "FIRESTARTER_ADMIN_PASSWORD"
# FIRESTARTER_DEMO=1 marks a public demo instance: the bootstrap admin keeps its
# password and is not forced to change it, so a visitor can log in with the
# documented credentials. Never set it on a real deployment.
DEMO_ENV = "FIRESTARTER_DEMO"


def hash_password(password: str) -> str:
    """Return a salted hash of *password* (scrypt via werkzeug)."""
    return generate_password_hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Return True if *password* matches the stored *password_hash*."""
    return check_password_hash(password_hash, password)


# ---------------------------------------------------------------------------
# Bootstrapping
# ---------------------------------------------------------------------------


def seed_user_roles(session: Session) -> None:
    """Ensure the default roles exist. Idempotent."""
    for name in DEFAULT_ROLES:
        if get_user_role_by_name(session, name) is None:
            session.add(UserRole(name=name))
    session.flush()


def bootstrap_admin_password() -> str:
    """The bootstrap admin's password: ``FIRESTARTER_ADMIN_PASSWORD`` or the default."""
    return os.getenv(ADMIN_PASSWORD_ENV) or DEFAULT_ADMIN_PASSWORD


def is_demo_mode() -> bool:
    """True when ``FIRESTARTER_DEMO`` is set to a truthy value (``1``, ``true``, ``yes``)."""
    return os.getenv(DEMO_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def seed_default_admin(session: Session) -> None:
    """Create the bootstrap ``admin`` account if no users exist yet.

    The account uses the bootstrap password (:func:`bootstrap_admin_password`) and
    is forced to change it on first login, so a fresh deployment is reachable
    without leaving a permanent known credential. In demo mode
    (:func:`is_demo_mode`) the forced change is skipped — the whole point of the
    demo is that anyone can log in with the documented credentials. No-op once any
    user exists.
    """
    if count_users(session) > 0:
        return
    seed_user_roles(session)
    user = create_user(
        session,
        username=DEFAULT_ADMIN_USERNAME,
        role_name=ADMIN_ROLE,
        password=bootstrap_admin_password(),
    )
    if is_demo_mode():
        user.must_change_password = False
        session.flush()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def authenticate(session: Session, *, username: str, password: str) -> User | None:
    """Return the User if *username*/*password* are valid, else None.

    On success ``last_login_at`` is stamped. Returns None for both unknown
    usernames and wrong passwords (callers must not leak which it was).
    """
    user = get_user_by_username(session, username)
    if user is None or not verify_password(user.password_hash, password):
        return None
    user.last_login_at = datetime.now(UTC)
    session.flush()
    return user


# ---------------------------------------------------------------------------
# Mutations (admin-driven unless noted)
# ---------------------------------------------------------------------------


def create_user(session: Session, *, username: str, role_name: str, password: str) -> User:
    """Create a user with a temporary password (must be changed on first login).

    Raises ValueError if the username is taken or the role is unknown.
    """
    username = username.strip()
    if not username:
        raise ValueError("Username must not be empty.")
    if not password:
        raise ValueError("Password must not be empty.")
    if get_user_by_username(session, username) is not None:
        raise ValueError(f"User {username!r} already exists.")

    role = get_user_role_by_name(session, role_name)
    if role is None:
        raise ValueError(f"Unknown role {role_name!r}.")

    user = User(
        username=username,
        password_hash=hash_password(password),
        must_change_password=True,
        role=role,
    )
    session.add(user)
    session.flush()
    return user


def change_password(session: Session, *, user_id: int, new_password: str) -> None:
    """Set a new password for *user_id* and clear the forced-change flag.

    This is the self-service path (the user changing their own password), so it
    also satisfies the first-login forced-change requirement.
    """
    if not new_password:
        raise ValueError("Password must not be empty.")
    user = require(get_user_by_id(session, user_id), f"No user with id={user_id}")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    session.flush()


def reset_password(session: Session, *, user_id: int, temp_password: str) -> None:
    """Admin action: set a temporary password and force a change on next login."""
    if not temp_password:
        raise ValueError("Password must not be empty.")
    user = require(get_user_by_id(session, user_id), f"No user with id={user_id}")
    user.password_hash = hash_password(temp_password)
    user.must_change_password = True
    session.flush()


def set_role(session: Session, *, user_id: int, role_name: str) -> None:
    """Admin action: change a user's role.

    Refuses to demote the last remaining admin, which would lock everyone out
    of user management.
    """
    user = require(get_user_by_id(session, user_id), f"No user with id={user_id}")
    role = get_user_role_by_name(session, role_name)
    if role is None:
        raise ValueError(f"Unknown role {role_name!r}.")
    if user.role.name == ADMIN_ROLE and role.name != ADMIN_ROLE and count_admins(session) <= 1:
        raise ValueError("Cannot demote the last administrator.")
    user.role = role
    session.flush()


def delete_user(session: Session, *, user_id: int, acting_user_id: int) -> None:
    """Admin action: delete a user.

    Two safety rails: an admin cannot delete their own account (avoids
    accidental self-lockout mid-session), and the last administrator cannot be
    deleted (avoids locking everyone out of user management).
    """
    if user_id == acting_user_id:
        raise ValueError("You cannot delete your own account.")
    user = require(get_user_by_id(session, user_id), f"No user with id={user_id}")
    if user.role.name == ADMIN_ROLE and count_admins(session) <= 1:
        raise ValueError("Cannot delete the last administrator.")
    session.delete(user)
    session.flush()
