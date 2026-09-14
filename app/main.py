"""ASGI application entry point.

The application is exposed as ``app.main:app`` for Uvicorn, Hypercorn,
Gunicorn, and other ASGI servers.
"""
from __future__ import annotations

from fastapi import FastAPI


app = FastAPI(
    title="Museum Light Dose and Rotation API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)
