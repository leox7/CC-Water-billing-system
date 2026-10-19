"""Creates the app. Feature controllers, middleware and handlers are added here
by later tasks."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp

from app.core.config import Settings, get_settings
from app.core.database import get_session_factory
from app.core.deps import protect_by_default
from app.core.errors import register_error_handlers, render_error, unexpected_error
from app.core.logging import RequestContextMiddleware, configure_logging
from app.core.security import SecurityHeadersMiddleware, verify_csrf
from app.features.account import controller as account_controller
from app.features.audit import service as audit
from app.features.auth import controller as auth_controller
from app.features.bills import controller as bills_controller
from app.features.customers import controller as customers_controller
from app.features.dashboard import controller as dashboard_controller
from app.features.payments import controller as payments_controller
from app.features.readings import controller as readings_controller
from app.features.reports import controller as reports_controller

APP_FOLDER = Path(__file__).resolve().parent
PWA_FOLDER = APP_FOLDER / "shared" / "static"

logger = logging.getLogger("app.main")


def record_startup(settings: Settings) -> None:
    """AUDIT_LOG.md section 4: system.started says which SMS backend and environment are in use.

    A database that is down at startup must not stop the app from starting.
    """
    try:
        with get_session_factory()() as db:
            audit.record_event(
                db,
                "system.started",
                f"App started in {settings.app_env}, SMS backend {settings.sms_backend}",
                details={"sms_backend": settings.sms_backend, "environment": settings.app_env},
            )
            db.commit()
    except SQLAlchemyError:
        logger.error("The startup event could not be recorded", exc_info=True)


def mount_static_folders(app: FastAPI) -> None:
    """Shared files at /static/shared/, each feature's own CSS at /static/<feature>/.

    Only the static/ folders are served, never the Python files next to them.
    """
    app.mount("/static/shared", StaticFiles(directory=APP_FOLDER / "shared" / "static"), name="static-shared")
    for feature_folder in sorted((APP_FOLDER / "features").iterdir()):
        static_folder = feature_folder / "static"
        if static_folder.is_dir():
            app.mount(
                f"/static/{feature_folder.name}",
                StaticFiles(directory=static_folder),
                name=f"static-{feature_folder.name}",
            )


class WaterBillingApp(FastAPI):
    """FastAPI, with the security headers and the request ID wrapped around everything else.

    Starlette builds the response for an unhandled error (a 500) in its outermost layer,
    outside any middleware added with add_middleware. Wrapping the finished stack here is
    the one way to make sure that response carries the headers (SECURITY.md section 14)
    and the X-Request-ID too (AUD-4).
    """

    def __init__(self, hsts_seconds: int, **fastapi_options: Any) -> None:
        self.hsts_seconds = hsts_seconds
        super().__init__(**fastapi_options)

    def build_middleware_stack(self) -> ASGIApp:
        with_request_id = RequestContextMiddleware(super().build_middleware_stack())
        return SecurityHeadersMiddleware(with_request_id, self.hsts_seconds)


def create_app(settings: Settings) -> FastAPI:
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        record_startup(settings)
        yield

    # REQ-4, OUT-4: the interactive API pages exist only in debug mode
    app = WaterBillingApp(
        hsts_seconds=settings.hsts_seconds,
        lifespan=lifespan,
        title="C&C H2O Water Billing",
        # On every route, so a new route cannot forget them. First the sign in (AUTH-S1:
        # a forgotten route fails closed), then the CSRF token on every POST (CSRF-2).
        dependencies=[Depends(protect_by_default), Depends(verify_csrf)],
        debug=settings.debug,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        openapi_url="/openapi.json" if settings.debug else None,
    )

    # SES-1, SES-3: a signed cookie, HttpOnly, SameSite=Lax, Secure in production, fixed lifetime
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        max_age=settings.session_lifetime_days * 24 * 60 * 60,
        same_site="lax",
        https_only=settings.is_production,
    )
    mount_static_folders(app)
    register_error_handlers(app)
    app.include_router(auth_controller.router)
    app.include_router(account_controller.router)
    app.include_router(customers_controller.router)
    app.include_router(readings_controller.router)
    app.include_router(bills_controller.router)
    app.include_router(payments_controller.router)
    app.include_router(reports_controller.router)
    app.include_router(dashboard_controller.router)

    @app.get("/health", response_class=PlainTextResponse)
    def health() -> str:
        # No login needed (AUTH-S1). It says "ok" and nothing else, so an uptime
        # monitor can use it without learning anything about the system.
        return "ok"

    # The PWA files are served from the root path: a service worker controls only the pages
    # under its own address, so /sw.js controls the whole app. Both are public, because the
    # browser fetches them without the session cookie.
    @app.get("/sw.js")
    def service_worker(request: Request) -> Response:
        try:
            # no-cache: the browser checks for a new version every time, so an update is seen
            return FileResponse(PWA_FOLDER / "sw.js", media_type="text/javascript",
                                headers={"Cache-Control": "no-cache"})
        except Exception as exc:
            return render_error(request, unexpected_error(exc, "load the app files"))

    @app.get("/manifest.json")
    def web_manifest(request: Request) -> Response:
        try:
            return FileResponse(PWA_FOLDER / "manifest.json", media_type="application/manifest+json")
        except Exception as exc:
            return render_error(request, unexpected_error(exc, "load the app files"))

    return app


app = create_app(get_settings())
