"""ASGI entry point: ``uvicorn gcc_core.main:app``."""

from .app import create_app

app = create_app()
