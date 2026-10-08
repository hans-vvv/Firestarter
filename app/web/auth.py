"""Flask request-context auth helpers: session identity and route guards.

The logged-in identity is kept in Flask's signed ``session`` cookie as a small
dict (id / username / role / must_change_password). Reading it is therefore a
per-request operation with no database hit — the DB is touched only at login,
password change, and admin actions. This keeps the existing route tests (which
deliberately make no DB calls) working: they just pre-set the session.

The role name is cached in the cookie, so an admin changing another user's role
only takes effect after that user logs in again — acceptable while roles are
display-only this sprint.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import flash, redirect, request, session, url_for
from werkzeug.wrappers import Response

from app.web.accounts import ADMIN_ROLE

SESSION_KEY = "user"

# Roles permitted to mutate state (edit service definitions, replace the input
# workbook, …). Read-only users may view but not change. This is the single
# source of truth for the write gate so individual blueprints don't each
# re-declare the role set.
WRITE_ROLES = frozenset({"rw", ADMIN_ROLE})


def login_user(*, user_id: int, username: str, role: str, must_change_password: bool) -> None:
    """Store the authenticated identity in the session cookie."""
    session[SESSION_KEY] = {
        "id": user_id,
        "username": username,
        "role": role,
        "must_change_password": must_change_password,
    }


def logout_user() -> None:
    """Clear the authenticated identity from the session."""
    session.pop(SESSION_KEY, None)


def current_user() -> dict[str, Any] | None:
    """Return the logged-in identity dict, or None if not authenticated."""
    return session.get(SESSION_KEY)


def mark_password_changed() -> None:
    """Clear the forced-change flag on the in-session identity after a change."""
    user = session.get(SESSION_KEY)
    if user is not None:
        user["must_change_password"] = False
        session[SESSION_KEY] = user


def is_admin() -> bool:
    """Return True if the current user holds the admin role."""
    user = current_user()
    return user is not None and user.get("role") == ADMIN_ROLE


def can_write() -> bool:
    """Return True if the current user may perform write/mutating actions."""
    user = current_user()
    return user is not None and user.get("role") in WRITE_ROLES


def login_required(view: Callable) -> Callable:
    """Redirect anonymous callers to the login page, preserving the target."""

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if current_user() is None:
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view: Callable) -> Callable:
    """Allow only admins; anonymous → login, non-admin → 403."""

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if current_user() is None:
            return redirect(url_for("auth.login", next=request.path))
        if not is_admin():
            flash("Administrator access required.", "danger")
            return Response("Forbidden — administrator access required.", status=403)
        return view(*args, **kwargs)

    return wrapped
