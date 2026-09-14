"""Spectral action-function mathematics.

Given a light source spectrum (relative power sampled at wavelengths) and an
exhibit material's damage-action (sensitivity) curve, the service converts
raw illumination in lux-hours into *equivalent damage*:

    factor = integral_over_common( P_norm(lambda) * S(lambda) d lambda )
    equivalent_damage = factor * lux_hours

``P_norm`` is the source spectrum normalized so that its integral over its own
span is one; ``S`` is used as submitted. A flat sensitivity curve ``S = 1``
therefore yields ``factor = 1`` and equivalent damage numerically equal to the
lux-hours, which anchors the spectral units to the legacy lux-hour accounting.

Both curves are linearly interpolated on the union wavelength grid restricted
to their common range. Per-nanometre cells are aggregated into named bands.
Wavelength intervals that only one curve covers are reported as coverage gaps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import Exhibit, IlluminationSegment, SpectrumCurve

# Named wavelength bands (nanometres). A grid cell is assigned by its midpoint.
BAND_EDGES_NM: tuple[tuple[str, float, float], ...] = (
    ("ultraviolet", float("-inf"), 400.0),
    ("violet_blue", 400.0, 500.0),
    ("green", 500.0, 600.0),
    ("red", 600.0, 700.0),
    ("near_infrared", 700.0, float("inf")),
)


@dataclass
class MissingRange:
    start_nm: float
    end_nm: float
    missing: str  # "source" | "sensitivity" — which curve lacks samples here


@dataclass
class CellContribution:
    start_nm: float
    end_nm: float
    damage: float  # integral of P_norm * S over the cell (factor units)


@dataclass
class BandAggregate:
    name: str
    start_nm: float
    end_nm: float
    damage: float


@dataclass
class SpectralEvaluation:
    factor: float
    cells: list[CellContribution]
    missing_ranges: list[MissingRange]
    common_start_nm: float
    common_end_nm: float
    light_coverage: float
    sensitivity_coverage: float

    def bands(self) -> list[BandAggregate]:
        """Aggregate adjacent grid cells into named wavelength bands."""
        grouped: dict[str, BandAggregate] = {}
        for cell in self.cells:
            midpoint = (cell.start_nm + cell.end_nm) / 2.0
            name = next(
                band
                for band, low, high in BAND_EDGES_NM
                if low <= midpoint < high
            )
            aggregate = grouped.get(name)
            if aggregate is None:
                grouped[name] = BandAggregate(name, cell.start_nm, cell.end_nm, cell.damage)
            else:
                aggregate.damage += cell.damage
                aggregate.start_nm = min(aggregate.start_nm, cell.start_nm)
                aggregate.end_nm = max(aggregate.end_nm, cell.end_nm)
        return [grouped[name] for name, _, _ in BAND_EDGES_NM if name in grouped]


@dataclass
class SpectralFailure:
    """A spectrum pair that cannot produce an equivalent-damage factor."""

    code: str
    message: str
    missing_ranges: list[MissingRange] = field(default_factory=list)


@dataclass
class _Curve:
    wavelengths: list[float]
    values: list[float]

    @property
    def lo(self) -> float:
        return self.wavelengths[0]

    @property
    def hi(self) -> float:
        return self.wavelengths[-1]

    def value_at(self, wavelength: float) -> float:
        """Linear interpolation; extrapolation must not be requested."""
        wavelengths = self.wavelengths
        if wavelength <= wavelengths[0]:
            return self.values[0]
        if wavelength >= wavelengths[-1]:
            return self.values[-1]
        # Binary search for the surrounding sample pair.
        low_idx, high_idx = 0, len(wavelengths) - 1
        while high_idx - low_idx > 1:
            mid_idx = (low_idx + high_idx) // 2
            if wavelengths[mid_idx] <= wavelength:
                low_idx = mid_idx
            else:
                high_idx = mid_idx
        x0, x1 = wavelengths[low_idx], wavelengths[high_idx]
        y0, y1 = self.values[low_idx], self.values[high_idx]
        if x1 == x0:
            return y0
        return y0 + (y1 - y0) * (wavelength - x0) / (x1 - x0)


def _to_curve(curve: SpectrumCurve) -> _Curve:
    return _Curve(
        wavelengths=[float(w) for w in curve.wavelengths_nm],
        values=[float(v) for v in curve.values],
    )


def _trapezoid(values: list[float], grid: list[float]) -> float:
    return sum(
        (grid[idx + 1] - grid[idx]) * (values[idx] + values[idx + 1]) / 2.0
        for idx in range(len(grid) - 1)
    )


def evaluate_spectra(
    segment: IlluminationSegment,
    exhibit: Exhibit,
) -> SpectralEvaluation | SpectralFailure:
    """Evaluate the source/action pair for one illuminated segment.

    Requires both a source spectrum on the segment and a sensitivity curve
    (with ``equivalent_damage_limit``) on the exhibit.
    """
    source_curve = segment.spectrum
    sensitivity_curve = exhibit.sensitivity
    if source_curve is None or sensitivity_curve is None:  # pragma: no cover - guarded by caller
        return SpectralFailure("spectrum_unavailable", "missing spectral data")

    light = _to_curve(source_curve)
    sensitivity = _to_curve(sensitivity_curve)

    light_area = _trapezoid(light.values, light.wavelengths)
    sensitivity_area = _trapezoid(sensitivity.values, sensitivity.wavelengths)
    if light_area <= 0.0:
        return SpectralFailure(
            "spectrum_zero_power",
            "source spectrum integrates to zero relative power; "
            "cannot normalize and derive an equivalent-damage factor",
        )
    if sensitivity_area <= 0.0:
        return SpectralFailure(
            "spectrum_zero_sensitivity",
            "sensitivity curve integrates to zero; equivalent damage is undefined",
        )

    common_lo = max(light.lo, sensitivity.lo)
    common_hi = min(light.hi, sensitivity.hi)
    if common_hi <= common_lo:
        missing = [
            MissingRange(light.lo, light.hi, "sensitivity"),
            MissingRange(sensitivity.lo, sensitivity.hi, "source"),
        ]
        missing.sort(key=lambda gap: (gap.start_nm, gap.end_nm))
        return SpectralFailure(
            "spectrum_no_overlap",
            "source spectrum and sensitivity curve share no wavelength range",
            missing_ranges=missing,
        )

    # Union grid restricted to the common wavelength range, augmented with the
    # named-band boundaries so no integration cell crosses a band edge. Both
    # curves are linearly interpolated on every grid node.
    band_boundaries = [
        edge
        for _, low, high in BAND_EDGES_NM
        for edge in (low, high)
        if edge not in (float("-inf"), float("inf"))
    ]
    nodes = sorted(
        {
            wavelength
            for wavelength in light.wavelengths
            + sensitivity.wavelengths
            + band_boundaries
            if common_lo <= wavelength <= common_hi
        }
    )
    normalized_light = [light.value_at(node) / light_area for node in nodes]
    sensitivity_values = [sensitivity.value_at(node) for node in nodes]
    products = [p * s for p, s in zip(normalized_light, sensitivity_values)]
    factor = _trapezoid(products, nodes)

    cells = [
        CellContribution(
            start_nm=nodes[idx],
            end_nm=nodes[idx + 1],
            damage=(nodes[idx + 1] - nodes[idx])
            * (products[idx] + products[idx + 1])
            / 2.0,
        )
        for idx in range(len(nodes) - 1)
    ]

    missing_ranges: list[MissingRange] = []
    if sensitivity.lo < common_lo:
        missing_ranges.append(MissingRange(sensitivity.lo, common_lo, "source"))
    if light.lo < common_lo:
        missing_ranges.append(MissingRange(light.lo, common_lo, "sensitivity"))
    if sensitivity.hi > common_hi:
        missing_ranges.append(MissingRange(common_hi, sensitivity.hi, "source"))
    if light.hi > common_hi:
        missing_ranges.append(MissingRange(common_hi, light.hi, "sensitivity"))
    missing_ranges.sort(key=lambda gap: (gap.start_nm, gap.end_nm))

    # Fraction of each curve's own area lying inside the common range.
    covered_nodes = [node for node in nodes]
    light_covered_area = _trapezoid(
        [light.value_at(node) for node in covered_nodes], covered_nodes
    )
    sensitivity_covered_area = _trapezoid(
        [sensitivity.value_at(node) for node in covered_nodes], covered_nodes
    )

    return SpectralEvaluation(
        factor=factor,
        cells=cells,
        missing_ranges=missing_ranges,
        common_start_nm=common_lo,
        common_end_nm=common_hi,
        light_coverage=light_covered_area / light_area,
        sensitivity_coverage=sensitivity_covered_area / sensitivity_area,
    )


class SpectralEvaluator:
    """Caches per-(segment, exhibit) spectral evaluations for a request."""

    def __init__(self) -> None:
        self._cache: dict[tuple[int, int], SpectralEvaluation | SpectralFailure] = {}

    def evaluate(
        self, segment: IlluminationSegment, exhibit: Exhibit
    ) -> SpectralEvaluation | SpectralFailure:
        key = (id(segment), id(exhibit.sensitivity))
        cached = self._cache.get(key)
        if cached is None:
            cached = evaluate_spectra(segment, exhibit)
            self._cache[key] = cached
        return cached

def decimal_or_none(value: float | None, places: str = "0.000000") -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal(places))
