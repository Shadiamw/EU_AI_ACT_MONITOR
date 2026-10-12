"""STUB for Part 1 (main): a minimal app with the Part 2 router and the error handlers only."""

from fastapi import FastAPI

from app import errors
from app.api import ledger_routes


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(ledger_routes.router)
    errors.install(app)
    return app


app = create_app()
