"""Flask blueprint for authentication: login, logout, forced password change."""

from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask.typing import ResponseReturnValue

from app.web.audit import audit_event, set_audit_context
from app.web.auth import current_user, login_user, logout_user, mark_password_changed
from app.web.utils import authenticate_user, change_user_password

bp = Blueprint("auth", __name__)


def _safe_next(target: str | None) -> str:
    """Return a safe local redirect target, or the overview as a fallback.

    Only same-site relative paths are allowed (must start with a single ``/``)
    so a crafted ``?next=`` cannot bounce the user to an external site after
    login (open-redirect guard).
    """
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("overview.index")


@bp.get("/login")
def login() -> ResponseReturnValue:
    """Show the login form (or skip it if already authenticated)."""
    if current_user() is not None:
        return redirect(url_for("overview.index"))
    return render_template("login.html", next=request.args.get("next", ""))


@bp.post("/login")
def login_post() -> ResponseReturnValue:
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = authenticate_user(username=username, password=password)
    if user is None:
        # Deliberately vague to the browser — do not reveal whether the username
        # exists — but the audit trail records the attempted username (never the
        # password) so repeated failures are visible.
        set_audit_context(event="auth.login", result="fail", attempted_user=username)
        flash("Invalid username or password.", "danger")
        return render_template("login.html", next=request.form.get("next", "")), 401

    login_user(
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        must_change_password=user["must_change_password"],
    )
    set_audit_context(event="auth.login", result="ok")
    if user["must_change_password"]:
        flash("Please choose a new password before continuing.", "warning")
        return redirect(url_for("auth.change_password"))
    return redirect(_safe_next(request.form.get("next")))


@bp.post("/logout")
def logout() -> ResponseReturnValue:
    # Record while the identity is still in session — logout_user() clears it, so
    # the automatic after_request line would otherwise read anonymous.
    audit_event("auth.logout")
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))


@bp.get("/change-password")
def change_password() -> ResponseReturnValue:
    if current_user() is None:
        return redirect(url_for("auth.login"))
    return render_template("change_password.html")


@bp.post("/change-password")
def change_password_post() -> ResponseReturnValue:
    user = current_user()
    if user is None:
        return redirect(url_for("auth.login"))

    new_password = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    error = None
    if len(new_password) < 8:
        error = "Password must be at least 8 characters."
    elif new_password != confirm:
        error = "Passwords do not match."

    if error is not None:
        flash(error, "danger")
        return render_template("change_password.html"), 400

    change_user_password(user_id=user["id"], new_password=new_password)
    mark_password_changed()
    set_audit_context(event="auth.password_change", result="ok")
    flash("Password updated.", "success")
    return redirect(url_for("overview.index"))
