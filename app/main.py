"""Application factory: ingestion, events, ledger, webhooks, health, UI and test-only routes."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import config, errors, observability
from app.api import (
    harness_routes,
    health_routes,
    ingestion_routes,
    ledger_routes,
    stub_routes,
    test_routes,
    webhook_routes,
)

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(ingestion_routes.router)
    app.include_router(ledger_routes.router)
    app.include_router(webhook_routes.router)
    app.include_router(stub_routes.router)
    app.include_router(health_routes.router)
    app.include_router(test_routes.router)
    # Staff autograder bases: .../system and .../tester (+ /test/setup|run|report).
    app.include_router(harness_routes.build_router("system"), prefix="/system")
    app.include_router(harness_routes.build_router("tester"), prefix="/tester")

    @app.get("/")
    def ui_home() -> RedirectResponse:
        return RedirectResponse(url="/ui/", status_code=307)

    # html=True so /ui/ serves index.html and relative ./app.css resolves under /ui/.
    app.mount("/ui", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")

    errors.install(app)
    # Outermost, so the request log line also covers the 500 body from errors.install.
    app.add_middleware(observability.RequestLogMiddleware)
    origins = config.cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    observability.configure_logging(config.log_level())
    return app


app = create_app()
