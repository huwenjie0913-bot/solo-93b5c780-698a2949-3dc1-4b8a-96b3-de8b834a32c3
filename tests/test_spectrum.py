"""Spectral action integral: normalization, interpolation, bands, gaps."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.models import Exhibit, IlluminationSegment, SpectrumCurve
from app.spectrum import evaluate_spectra


def _segment(ws, vs, lux="100"):
    return IlluminationSegment(
        start=datetime(2026, 1, 1, 9), end=datetime(2026, 1, 1, 17),
        lux=Decimal(lux),
        spectrum=SpectrumCurve(wavelengths_nm=ws, values=vs),
    )


def _exhibit(ws, vs, limit="50"):
    return Exhibit(
        id="x", dose_limit_lux_hours=Decimal("1000"),
        sensitivity=SpectrumCurve(wavelengths_nm=ws, values=vs),
        equivalent_damage_limit=Decimal(limit),
    )


def test_flat_sensitivity_gives_unit_factor():
    result = evaluate_spectra(
        _segment(("400", "700"), ("1", "1")),
        _exhibit(("400", "700"), ("1", "1")),
    )
    assert abs(result.factor - 1.0) < 1e-12
    # flat normalized power = 1/300 per nm; equal band widths share damage
    bands = {b.name: b.damage for b in result.bands()}
    assert set(bands) == {"violet_blue", "green", "red"}
    assert all(abs(v - 1 / 3) < 1e-9 for v in bands.values())


def test_led_and_daylight_equal_luxhours_differ_in_damage():
    sensitivity = _exhibit(("400", "700"), ("1", "0.2"))
    led = evaluate_spectra(
        _segment(("400", "440", "460", "700"), ("0.05", "1", "1", "0.05")),
        sensitivity,
    )
    daylight = evaluate_spectra(
        _segment(("400", "700"), ("1", "1")), sensitivity
    )
    assert led.factor > daylight.factor
    # the blue-leaning LED puts more weight into the violet_blue band
    led_bands = {b.name: b.damage for b in led.bands()}
    assert led_bands["violet_blue"] > led_bands["red"]


def test_normalization_is_independent_of_power_units():
    sensitivity = _exhibit(("400", "700"), ("1", "1"))
    scaled = evaluate_spectra(_segment(("400", "700"), ("10", "10")), sensitivity)
    base = evaluate_spectra(_segment(("400", "700"), ("1", "1")), sensitivity)
    assert abs(scaled.factor - base.factor) < 1e-12


def test_coverage_gap_reports_missing_ranges_on_both_sides():
    result = evaluate_spectra(
        _segment(("300", "500"), ("1", "1")),
        _exhibit(("400", "700"), ("1", "1")),
    )
    missing = {(g.start_nm, g.end_nm, g.missing) for g in result.missing_ranges}
    assert (300.0, 400.0, "sensitivity") in missing
    assert (500.0, 700.0, "source") in missing
    assert result.common_start_nm == 400.0 and result.common_end_nm == 500.0
    assert abs(result.light_coverage - 0.5) < 1e-9
    assert abs(result.sensitivity_coverage - 1 / 3) < 1e-9


def test_no_overlap_is_failure_with_ranges():
    failure = evaluate_spectra(
        _segment(("300", "350"), ("1", "1")),
        _exhibit(("400", "700"), ("1", "1")),
    )
    assert failure.code == "spectrum_no_overlap"
    assert len(failure.missing_ranges) == 2


def test_zero_power_and_zero_sensitivity_failures():
    zero_power = evaluate_spectra(
        _segment(("400", "700"), ("0", "0")),
        _exhibit(("400", "700"), ("1", "1")),
    )
    assert zero_power.code == "spectrum_zero_power"
    zero_sens = evaluate_spectra(
        _segment(("400", "700"), ("1", "1")),
        _exhibit(("400", "700"), ("0", "0")),
    )
    assert zero_sens.code == "spectrum_zero_sensitivity"


def test_band_fractions_partition_the_integral():
    result = evaluate_spectra(
        _segment(("400", "440", "460", "700"), ("0.05", "1", "1", "0.05")),
        _exhibit(("400", "700"), ("1", "0.2")),
    )
    band_sum = sum(b.damage for b in result.bands())
    assert abs(band_sum - result.factor) < 1e-9
