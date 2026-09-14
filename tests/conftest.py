"""Shared pytest fixtures and request builders."""
from __future__ import annotations

from typing import Callable

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def make_gallery() -> Callable:
    def _make(
        gallery_id="G1",
        *,
        illumination=None,
        capacity=1,
        weekly_open=True,
        timezone="UTC",
        candidate_gallery: bool = False,
    ):
        return {
            "id": gallery_id,
            "timezone": timezone,
            "capacity": capacity,
            "weekly_open": [
                {"weekday": day, "open_time": "09:00", "close_time": "17:00"}
                for day in range(7)
            ]
            if weekly_open
            else [],
            "illumination": illumination
            if illumination is not None
            else [
                {
                    "start": "2026-01-01T09:00:00Z",
                    "end": "2026-01-01T17:00:00Z",
                    "lux": "100",
                }
            ],
        }

    return _make


@pytest.fixture()
def make_segment() -> Callable:
    def _make(start="2026-01-01T09:00:00Z", end="2026-01-01T17:00:00Z",
              lux="100", spectrum=None):
        segment = {"start": start, "end": end, "lux": lux}
        if spectrum is not None:
            segment["spectrum"] = spectrum
        return segment

    return _make


@pytest.fixture()
def flat_spectrum() -> Callable:
    def _flat(ws=("400", "700"), values=("1", "1")):
        return {"wavelengths_nm": list(ws), "values": list(values)}

    return _flat


@pytest.fixture()
def spectral_exhibit_kwargs(flat_spectrum) -> Callable:
    def _kwargs(exhibit_id="P1", sensitivity_values=("1", "1"),
                sensitivity_ws=("400", "700"), damage_limit="600",
                lux_limit="100000", historical_damage="0",
                historical_dose="0"):
        return {
            "id": exhibit_id,
            "material": "photograph",
            "dose_limit_lux_hours": lux_limit,
            "historical_dose_lux_hours": historical_dose,
            "historical_equivalent_damage": historical_damage,
            "sensitivity": {
                "wavelengths_nm": list(sensitivity_ws),
                "values": list(sensitivity_values),
            },
            "equivalent_damage_limit": damage_limit,
        }

    return _kwargs
