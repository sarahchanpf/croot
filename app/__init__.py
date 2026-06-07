"""Flask app factory.

`api/index.py` does `from app import app`, so we expose a module-level `app`
built by `create_app()`. Blueprints live in `app/routes/`; business logic lives
in `app/core/` and is deliberately framework-free (no Flask imports) so it's
unit-testable without a request context.
"""

from datetime import timedelta
import os

from flask import Flask, render_template

from . import config
from .db import init_db


def create_app() -> Flask:
    flask_app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )
    flask_app.secret_key = config.SESSION_SECRET
    flask_app.config.update(
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
    )

    try:
        init_db()
    except Exception:
        # Best-effort: on a read-only fs the cache layer is simply unavailable.
        pass

    @flask_app.route("/")
    def index():
        return render_template("index.html")

    # Register route blueprints.
    from .routes.chat import bp as chat_bp
    from .routes.search import bp as search_bp
    from .routes.profile import bp as profile_bp
    from .routes.history import bp as history_bp
    from .routes.export import bp as export_bp
    from .routes.access import bp as access_bp

    for bp in (chat_bp, search_bp, profile_bp, history_bp, export_bp, access_bp):
        flask_app.register_blueprint(bp)

    return flask_app


app = create_app()
