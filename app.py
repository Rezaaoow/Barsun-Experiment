"""Compatibility entry point for platforms that expect app.py.

The FastAPI application is defined in main.py; this module re-exports it
so platforms can use either `app:app` or `main:app` as the ASGI target.
"""
from main import app
