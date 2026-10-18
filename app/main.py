"""Application factory: ingestion, events, ledger, webhooks, health and test-only routes."""

from fastapi import FastAPI

from app import config, errors, observability
from app.api import (
    health_routes,
    ingestion_routes,
    ledger_routes,
    stub_routes,
    test_routes,
    webhook_routes,
)


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(ingestion_routes.router)
    app.include_router(ledger_routes.router)
    app.include_router(webhook_routes.router)
    app.include_router(stub_routes.router)
    app.include_router(health_routes.router)
    app.include_router(test_routes.router)
    errors.install(app)
    # Outermost, so the request log line also covers the 500 body from errors.install.
    app.add_middleware(observability.RequestLogMiddleware)
    observability.configure_logging(config.log_level())
    return app


app = create_app()
