from __future__ import annotations

import os

from flask import Flask, redirect, request, url_for
from werkzeug.wrappers import Response

import app.models  # noqa: F401 — register ORM tables on Base.metadata for create_all
from app.db.base import Base
from app.db.engine import engine
from app.logging.logger import setup_audit_logging, setup_logging
from app.utils import db_session
from app.web import accounts
from app.web.audit import register_audit
from app.web.auth import current_user, is_admin
from app.web.routes.admin import bp as admin_bp
from app.web.routes.api import bp as api_bp
from app.web.routes.auth_routes import bp as auth_bp
from app.web.routes.compliance import bp as compliance_bp
from app.web.routes.compliance_exceptions import bp as compliance_exceptions_bp
from app.web.routes.data_bundle import bp as data_bundle_bp
from app.web.routes.devices import bp as devices_bp
from app.web.routes.excel_data import bp as excel_data_bp
from app.web.routes.jobs import bp as jobs_bp
from app.web.routes.latest_backups import bp as latest_backups_bp
from app.web.routes.overview import bp as overview_bp
from app.web.routes.pipeline import bp as pipeline_bp
from app.web.routes.remediations import bp as remediations_bp
from app.web.routes.services import bp as services_bp
from app.web.routes.snippets import bp as snippets_bp


def ensure_schema() -> None:
    """Create any missing tables (idempotent; never drops or alters).

    Lets a fresh deployment self-heal — the ``users``/``user_role`` tables are
    created on first run without a manual migration step. Proper migrations
    (Alembic) are a separate, production-time concern.
    """
    Base.metadata.create_all(bind=engine)


def bootstrap_auth() -> None:
    """Seed reference data + the bootstrap admin account. Idempotent.

    Covers the auth tables (roles + bootstrap admin), so a fresh deployment is
    immediately usable without a manual seeding step.
    """
    ensure_schema()
    with db_session() as session:
        accounts.seed_user_roles(session)
        accounts.seed_default_admin(session)


def create_app(*, bootstrap: bool = True) -> Flask:
    """Create and configure the Flask application.

    ``bootstrap`` runs schema creation + role/admin seeding against the
    configured database. Route tests pass ``bootstrap=False`` so importing the
    app never touches the real database (they pre-set the session instead).
    """
    # Initialise logging first, so anything create_app does (bootstrap, seeding)
    # and every subsequent request is captured. Both are idempotent. This is the
    # only place the dashboard sets logging up — gunicorn imports this factory,
    # not run.py/main.py, so without it the app logger and the transaction/audit
    # log would never be configured.
    setup_logging()
    setup_audit_logging()

    app = Flask(__name__, template_folder="templates", static_folder="static")
    # Signs the session cookie that carries the logged-in identity, so it must
    # be a stable secret in production — override via the SECRET_KEY env var.
    app.secret_key = os.getenv("SECRET_KEY", "firestarter-dev")

    register_audit(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(overview_bp)
    app.register_blueprint(pipeline_bp)
    app.register_blueprint(devices_bp)
    app.register_blueprint(latest_backups_bp)
    app.register_blueprint(compliance_bp)
    app.register_blueprint(compliance_exceptions_bp)
    app.register_blueprint(remediations_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(services_bp)
    app.register_blueprint(snippets_bp)
    app.register_blueprint(excel_data_bp)
    app.register_blueprint(data_bundle_bp)

    @app.context_processor
    def _inject_user() -> dict:
        """Expose the logged-in identity to every template (navbar, gating)."""
        return {"current_user": current_user(), "is_admin": is_admin()}

    @app.before_request
    def _require_login() -> Response | None:
        """Gate every page behind authentication and the forced first change.

        Allow-listed without a session: the ``auth`` blueprint (login / logout /
        change-password) and static assets. An authenticated user still carrying
        ``must_change_password`` is funnelled to the change-password page until
        they comply.
        """
        if request.endpoint in (None, "static"):
            return None
        if request.blueprint == "auth":
            return None
        # The JSON API is intentionally unauthenticated (internal network only):
        # its callers are programs, which cannot follow a redirect to an HTML
        # login page. Keep it out of the session gate.
        if request.blueprint == "api":
            return None

        user = current_user()
        if user is None:
            return redirect(url_for("auth.login", next=request.path))
        if user.get("must_change_password"):
            return redirect(url_for("auth.change_password"))
        return None

    if bootstrap:
        bootstrap_auth()

    return app
