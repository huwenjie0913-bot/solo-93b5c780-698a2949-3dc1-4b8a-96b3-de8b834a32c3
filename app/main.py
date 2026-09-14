"""ASGI application entry point.

The application is exposed as ``app.main:app`` for Uvicorn, Hypercorn,
Gunicorn, and other ASGI servers.
"""
from __future__ import annotations

from fastapi import FastAPI

from .models import (
    DoseComputeRequest,
    DoseComputeResponse,
    ExposureReconcileRequest,
    ExposureReconcileResponse,
    HealthResponse,
    RotationRequest,
    RotationResponse,
)
from .reconcile import reconcile_exposure
from .scheduler import search_rotation
from .services import compute_dose

app = FastAPI(
    title="Museum Light Dose and Rotation API",
    version="1.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/dose", response_model=DoseComputeResponse, tags=["planning"])
def dose(request: DoseComputeRequest) -> DoseComputeResponse:
    """Compute accumulated lux-hours (and, when spectra are supplied,
    equivalent spectral damage) for fixed placements."""
    return DoseComputeResponse(**compute_dose(request))


@app.post("/rotation", response_model=RotationResponse, tags=["planning"])
def rotation(request: RotationRequest) -> RotationResponse:
    """Search a rotation schedule; candidate blocks are filtered by their
    equivalent-damage margin in spectral mode, by lux-hours otherwise."""
    return RotationResponse(**search_rotation(request))


@app.post(
    "/exposure-reconcile",
    response_model=ExposureReconcileResponse,
    tags=["reconciliation"],
)
def exposure_reconcile(request: ExposureReconcileRequest) -> ExposureReconcileResponse:
    """Reconcile measured sensor readings against the planned illumination:
    trapezoidal integration per placement with missing-data gaps cut out,
    per-exhibit measured/planned/delta lux-hours, coverage, corrected
    remaining dose and — with spectra — recomputed equivalent damage."""
    return ExposureReconcileResponse(**reconcile_exposure(request))
