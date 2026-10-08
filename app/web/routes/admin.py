"""Flask blueprint for the admin panel: dashboard user management.

Every route is gated by :func:`admin_required`. Mutating actions are htmx
form posts that return the refreshed user-table partial (with an optional
inline error banner), so the page updates in place without a full reload.
"""

from __future__ import annotations

from flask import Blueprint, abort, render_template, request

from app.web.audit import set_audit_context
from app.web.auth import admin_required, current_user
from app.web.utils import (
    create_user,
    delete_user,
    device_admin_password_is_set,
    list_user_roles,
    list_users,
    reset_user_password,
    set_device_admin_password,
    set_user_role,
)

bp = Blueprint("admin", __name__)


def _render_user_table(*, error: str | None = None) -> str:
    """Render the user-table partial with the current roster and roles."""
    me = current_user() or {}
    return render_template(
        "partials/user_table.html",
        users=list_users(),
        roles=list_user_roles(),
        current_user_id=me.get("id"),
        error=error,
    )


@bp.get("/admin")
@admin_required
def index() -> str:
    me = current_user() or {}
    return render_template(
        "admin.html",
        users=list_users(),
        roles=list_user_roles(),
        current_user_id=me.get("id"),
        password_is_set=device_admin_password_is_set(),
    )


@bp.post("/admin/users")
@admin_required
def create() -> str:
    username = request.form.get("username", "").strip()
    role = request.form.get("role", "")
    password = request.form.get("password", "")
    try:
        create_user(username=username, role_name=role, password=password)
    except ValueError as exc:
        set_audit_context(
            event="admin.user_create", target_user=username, role=role, result="error"
        )
        return _render_user_table(error=str(exc))
    set_audit_context(event="admin.user_create", target_user=username, role=role, result="ok")
    return _render_user_table()


@bp.post("/admin/users/<int:user_id>/role")
@admin_required
def change_role(user_id: int) -> str:
    role = request.form.get("role", "")
    try:
        set_user_role(user_id=user_id, role_name=role)
    except ValueError as exc:
        set_audit_context(
            event="admin.user_role", target_user_id=user_id, role=role, result="error"
        )
        return _render_user_table(error=str(exc))
    set_audit_context(event="admin.user_role", target_user_id=user_id, role=role, result="ok")
    return _render_user_table()


@bp.post("/admin/users/<int:user_id>/reset-password")
@admin_required
def reset_password(user_id: int) -> str:
    temp_password = request.form.get("temp_password", "")
    try:
        reset_user_password(user_id=user_id, temp_password=temp_password)
    except ValueError as exc:
        set_audit_context(event="admin.user_reset_password", target_user_id=user_id, result="error")
        return _render_user_table(error=str(exc))
    # Never log the temp password itself — only that a reset happened.
    set_audit_context(event="admin.user_reset_password", target_user_id=user_id, result="ok")
    return _render_user_table()


@bp.post("/admin/users/<int:user_id>/delete")
@admin_required
def delete(user_id: int) -> str:
    me = current_user()
    if me is None:  # @admin_required guarantees a session; narrow for the type checker
        abort(403)
    try:
        delete_user(user_id=user_id, acting_user_id=me["id"])
    except ValueError as exc:
        set_audit_context(event="admin.user_delete", target_user_id=user_id, result="error")
        return _render_user_table(error=str(exc))
    set_audit_context(event="admin.user_delete", target_user_id=user_id, result="ok")
    return _render_user_table()


@bp.post("/admin/device-password")
@admin_required
def set_device_password() -> str:
    """Record the production device password (admin only).

    Only the hash is kept, so this can never be read back — it exists to verify
    what an operator types, not to supply the password to anything. Setting it
    again replaces the previous value.
    """
    password = request.form.get("device_password", "")
    confirm = request.form.get("device_password_confirm", "")

    if not password:
        return _render_device_password_card(error="Enter a password.")
    if password != confirm:
        return _render_device_password_card(error="The two passwords do not match.")

    set_device_admin_password(password=password)
    # Records that the device password was set — never the value.
    set_audit_context(event="admin.device_password_set", result="ok")
    return _render_device_password_card(saved=True)


def _render_device_password_card(*, error: str | None = None, saved: bool = False) -> str:
    return render_template(
        "partials/device_password_card.html",
        password_is_set=device_admin_password_is_set(),
        error=error,
        saved=saved,
    )
