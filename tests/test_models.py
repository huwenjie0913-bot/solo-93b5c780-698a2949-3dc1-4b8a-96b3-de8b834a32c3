"""Pydantic-level validation of the spectral request contract."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import Exhibit, IlluminationSegment, SpectrumCurve
from datetime import datetime


def test_duplicate_wavelength_rejected():
    with pytest.raises(ValidationError) as excinfo:
        SpectrumCurve(wavelengths_nm=["400", "400", "500"], values=["1", "2", "3"])
    message = str(excinfo.value)
    assert "strictly increasing" in message


def test_out_of_order_wavelength_rejected():
    with pytest.raises(ValidationError):
        SpectrumCurve(wavelengths_nm=["500", "400"], values=["1", "2"])


def test_negative_wavelength_rejected():
    with pytest.raises(ValidationError):
        SpectrumCurve(wavelengths_nm=["-10", "500"], values=["1", "2"])


def test_negative_value_rejected():
    with pytest.raises(ValidationError):
        SpectrumCurve(wavelengths_nm=["400", "500"], values=["1", "-0.1"])


def test_length_mismatch_rejected():
    with pytest.raises(ValidationError):
        SpectrumCurve(wavelengths_nm=["400", "500", "600"], values=["1", "2"])


def test_too_few_samples_rejected():
    with pytest.raises(ValidationError):
        SpectrumCurve(wavelengths_nm=["400"], values=["1"])


def test_sensitivity_and_damage_limit_must_be_paired():
    curve = SpectrumCurve(wavelengths_nm=["400", "700"], values=["1", "1"])
    with pytest.raises(ValidationError, match="together"):
        Exhibit(id="x", dose_limit_lux_hours="10", sensitivity=curve)
    with pytest.raises(ValidationError, match="together"):
        Exhibit(id="x", dose_limit_lux_hours="10", equivalent_damage_limit="5")
    # both supplied is fine
    Exhibit(id="x", dose_limit_lux_hours="10",
            sensitivity=curve, equivalent_damage_limit="5")


def test_equivalent_damage_limit_must_be_positive():
    curve = SpectrumCurve(wavelengths_nm=["400", "700"], values=["1", "1"])
    with pytest.raises(ValidationError):
        Exhibit(id="x", dose_limit_lux_hours="10",
                sensitivity=curve, equivalent_damage_limit="0")


def test_segment_accepts_spectrum(flat_spectrum):
    segment = IlluminationSegment(
        start=datetime(2026, 1, 1), end=datetime(2026, 1, 2),
        lux="100", spectrum=flat_spectrum(),
    )
    assert segment.spectrum is not None
