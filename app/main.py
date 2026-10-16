"""STUB for Part 1 (main): a minimal app with the Part 2 and Part 3 routers and the error
handlers only."""

from fastapi import FastAPI

from app import errors
from app.api import health_routes, ledger_routes, webhook_routes


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(ledger_routes.router)
    app.include_router(webhook_routes.router)
    app.include_router(health_routes.router)
    errors.install(app)
    return app


app = create_app()
