"""Application factory for the ingestion, event, ledger and webhook APIs."""

from fastapi import FastAPI

from app import errors
from app.api import ingestion_routes, ledger_routes, webhook_routes


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(ingestion_routes.router)
    app.include_router(ledger_routes.router)
    app.include_router(webhook_routes.router)
    errors.install(app)
    return app


app = create_app()
