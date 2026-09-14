"""Measured-exposure reconciliation.

Sensor logs (actual illuminance) are reconciled against the planned
illumination schedule so conservation staff can replace plan-based dose
estimates with measured ones: dimming, drawn shades and temporary closures
all make the real exposure deviate from the calendar.

Adjacent readings within a placement are integrated with the trapezoidal
rule. Intervals longer than ``max_sampling_gap_hours`` are treated as
missing data: they are cut out of the integral (never interpolated across)
and reported as ``sampling_gap`` issues. Readings that fall outside every
placement are reported as ``unassigned_reading`` issues.

When the exhibit carries a sensitivity curve and the covering illumination
segments carry source spectra, each integrated piece is charged the
spectral equivalent-damage factor of the segment covering it (lux-weighted
when segments overlap); measured light outside every segment falls back to
the legacy 1:1 lux-hour rule and is flagged with
``measured_outside_illumination``.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from .engine import (
    QTY,
    RATIO,
    PlacementResult,
    _as_naive_utc,
    compute_placement,
    gallery_timezone,
    hours_between,
    local_days_between,
    make_issue,
    q,
)
from .models import (
    Exhibit,
    ExhibitReconcileResult,
    ExposureReconcileRequest,
    Gallery,
    Issue,
    Placement,
    PlacementReconcileResult,
    ReconcileDailyContribution,
    Severity,
    Status,
    TimeInterval,
)
from .services import validate_references
from .spectrum import SpectralEvaluation, SpectralEvaluator

INCOMPLETE_CODES = {"sampling_gap", "unassigned_reading"}


@dataclass
class _Reading:
    at: datetime  # naive UTC
    lux: Decimal


@dataclass
class _DayBucket:
    day: date
    day_start: datetime
    day_end: datetime
    covered_hours: Decimal = Decimal(0)
    lux_hours: Decimal = Decimal(0)
    equivalent_damage: Decimal = Decimal(0)
    readings: int = 0


@dataclass
class _PlacementReconcile:
    placement: Placement
    placement_index: int
    gallery: Gallery
    elapsed_hours: Decimal
    covered_hours: Decimal
    lux_hours: Decimal
    equivalent_damage: Decimal | None
    reading_count: int
    gaps: list[tuple[datetime, datetime]] = field(default_factory=list)
    days: list[_DayBucket] = field(default_factory=list)
    spectral_mode: bool = False


def _integrate_placement(
    placement: Placement,
    placement_index: int,
    gallery: Gallery,
    exhibit: Exhibit,
    readings: list[_Reading],
    *,
    max_gap_hours: Decimal,
    evaluator: SpectralEvaluator,
    issues: list[Issue],
) -> _PlacementReconcile:
    """Trapezoid-integrate one gallery's readings over one placement.

    ``readings`` holds the gallery's samples sorted by timestamp. Only
    samples inside [placement.start, placement.end] are used; adjacent
    pairs further apart than ``max_gap_hours`` are cut out of the integral.
    """
    tz, _ = gallery_timezone(gallery)  # validity already flagged by planned pass
    start = _as_naive_utc(placement.start)
    end = _as_naive_utc(placement.end)
    elapsed = hours_between(start, end)

    in_scope = [reading for reading in readings if start <= reading.at <= end]

    day_specs = local_days_between(start, end, tz)
    buckets = [
        _DayBucket(day=day, day_start=day_start, day_end=day_end)
        for day, day_start, day_end in day_specs
    ]
    day_starts = [bucket.day_start for bucket in buckets]

    def bucket_for(moment: datetime) -> _DayBucket:
        index = bisect_right(day_starts, moment) - 1
        index = min(max(index, 0), len(buckets) - 1)
        return buckets[index]

    spectral_exhibit = exhibit.sensitivity is not None
    split_points = {bucket.day_start for bucket in buckets}
    split_points.update(bucket.day_end for bucket in buckets)
    segments: list[tuple[datetime, datetime, object]] = []
    if spectral_exhibit:
        for segment in gallery.illumination:
            seg_start = _as_naive_utc(segment.start)
            seg_end = _as_naive_utc(segment.end)
            segments.append((seg_start, seg_end, segment))
            split_points.add(seg_start)
            split_points.add(seg_end)
    ordered_splits = sorted(split_points)

    gaps: list[tuple[datetime, datetime]] = []
    covered_hours = Decimal(0)
    lux_hours = Decimal(0)
    damage_total = Decimal(0)
    spectral_piece_count = 0
    outside_reported = False

    def segment_factor(segment) -> tuple[Decimal, bool]:
        """(damage factor, is_real_spectral) for one illumination segment."""
        if segment.spectrum is None:
            return Decimal(1), False
        outcome = evaluator.evaluate(segment, exhibit)
        if isinstance(outcome, SpectralEvaluation):
            return Decimal(str(outcome.factor)), True
        return Decimal(1), False  # failure already issued by the planned pass

    def piece_factor(midpoint: datetime) -> tuple[Decimal, bool]:
        nonlocal outside_reported
        covering = [
            segment
            for seg_start, seg_end, segment in segments
            if seg_start <= midpoint < seg_end
        ]
        if not covering:
            if not outside_reported:
                outside_reported = True
                issues.append(make_issue(
                    "measured_outside_illumination", Severity.RISK,
                    (f"measured light for exhibit '{exhibit.id}' in gallery "
                     f"'{gallery.id}' falls outside every illumination segment; "
                     "charging equivalent damage at the 1:1 lux-hour fallback"),
                    {"exhibit_id": exhibit.id, "gallery_id": gallery.id,
                     "placement_index": placement_index},
                ))
            return Decimal(1), False
        weighted = Decimal(0)
        weights = Decimal(0)
        factors = []
        real = False
        for segment in covering:
            factor, is_real = segment_factor(segment)
            factors.append(factor)
            real = real or is_real
            weighted += segment.lux * factor
            weights += segment.lux
        if weights > 0:
            return weighted / weights, real
        return sum(factors) / len(factors), real

    if not in_scope:
        gaps.append((start, end))
    else:
        if hours_between(start, in_scope[0].at) > max_gap_hours:
            gaps.append((start, in_scope[0].at))
        if hours_between(in_scope[-1].at, end) > max_gap_hours:
            gaps.append((in_scope[-1].at, end))
        for reading in in_scope:
            bucket_for(reading.at).readings += 1
        for first, second in zip(in_scope, in_scope[1:]):
            pair_hours = hours_between(first.at, second.at)
            if pair_hours > max_gap_hours:
                gaps.append((first.at, second.at))
                continue
            pair_seconds = Decimal((second.at - first.at).total_seconds())
            interior = [p for p in ordered_splits if first.at < p < second.at]
            points = [first.at, *interior, second.at]
            for piece_start, piece_end in zip(points, points[1:]):
                piece_seconds = Decimal((piece_end - piece_start).total_seconds())
                if piece_seconds <= 0:
                    continue
                # Linear interpolation between the two readings; splitting a
                # trapezoid of a linear function keeps the total area exact.
                lux_start = first.lux + (second.lux - first.lux) * (
                    Decimal((piece_start - first.at).total_seconds()) / pair_seconds
                )
                lux_end = first.lux + (second.lux - first.lux) * (
                    Decimal((piece_end - first.at).total_seconds()) / pair_seconds
                )
                piece_hours = hours_between(piece_start, piece_end)
                piece_lux_hours = (lux_start + lux_end) / 2 * piece_hours
                covered_hours += piece_hours
                lux_hours += piece_lux_hours
                bucket = bucket_for(piece_start)
                bucket.covered_hours += piece_hours
                bucket.lux_hours += piece_lux_hours
                if spectral_exhibit:
                    midpoint = piece_start + (piece_end - piece_start) / 2
                    factor, is_real = piece_factor(midpoint)
                    if is_real:
                        spectral_piece_count += 1
                    damage = factor * piece_lux_hours
                    damage_total += damage
                    bucket.equivalent_damage += damage

    # Same rule as the engine's mixed-placement fallback: equivalent damage
    # is only reported when at least one integrated piece had a real spectral
    # factor; the remaining pieces are then charged 1:1.
    spectral_mode = spectral_exhibit and spectral_piece_count > 0
    equivalent_damage = damage_total if spectral_mode else None

    for gap_start, gap_end in gaps:
        gap_hours = hours_between(gap_start, gap_end)
        if not in_scope:
            message = (
                f"no readings fall within the placement of exhibit "
                f"'{exhibit.id}' in gallery '{gallery.id}' "
                f"({gap_start.isoformat()}..{gap_end.isoformat()})"
            )
        else:
            message = (
                f"no measured lux integrated for exhibit '{exhibit.id}' in "
                f"gallery '{gallery.id}' between {gap_start.isoformat()} and "
                f"{gap_end.isoformat()} ({q(gap_hours)}h without usable readings)"
            )
        issues.append(make_issue(
            "sampling_gap", Severity.RISK, message,
            {"exhibit_id": exhibit.id, "gallery_id": gallery.id,
             "placement_index": placement_index,
             "gap_start": gap_start.isoformat(), "gap_end": gap_end.isoformat()},
            value=str(q(gap_hours)),
            limit=str(max_gap_hours),
        ))

    return _PlacementReconcile(
        placement=placement,
        placement_index=placement_index,
        gallery=gallery,
        elapsed_hours=elapsed,
        covered_hours=covered_hours,
        lux_hours=lux_hours,
        equivalent_damage=equivalent_damage,
        reading_count=len(in_scope),
        gaps=gaps,
        days=[b for b in buckets if b.readings > 0 or b.covered_hours > 0],
        spectral_mode=spectral_mode,
    )


def _placement_reconcile_model(
    rec: _PlacementReconcile, planned: PlacementResult
) -> PlacementReconcileResult:
    measured_damage = rec.equivalent_damage
    planned_damage = planned.equivalent_damage
    delta_damage = None
    if measured_damage is not None and planned_damage is not None:
        delta_damage = str(q(measured_damage - planned_damage, QTY))
    coverage = (
        rec.covered_hours / rec.elapsed_hours
        if rec.elapsed_hours > 0 else Decimal(0)
    )
    daily = [
        ReconcileDailyContribution(
            gallery_date=bucket.day.isoformat(),
            local_day_start=bucket.day_start,
            local_day_end=bucket.day_end,
            covered_hours=str(q(bucket.covered_hours)),
            lux_hours=str(q(bucket.lux_hours, QTY)),
            equivalent_damage=(
                str(q(bucket.equivalent_damage, QTY)) if rec.spectral_mode else None
            ),
            readings=bucket.readings,
        )
        for bucket in rec.days
    ]
    return PlacementReconcileResult(
        placement_index=rec.placement_index,
        placement_id=rec.placement.id,
        exhibit_id=rec.placement.exhibit_id,
        gallery_id=rec.placement.gallery_id,
        start=rec.placement.start,
        end=rec.placement.end,
        elapsed_hours=str(q(rec.elapsed_hours)),
        covered_hours=str(q(rec.covered_hours)),
        coverage_ratio=str(q(coverage, RATIO)),
        reading_count=rec.reading_count,
        measured_lux_hours=str(q(rec.lux_hours, QTY)),
        planned_lux_hours=str(q(planned.lux_hours, QTY)),
        delta_lux_hours=str(q(rec.lux_hours - planned.lux_hours, QTY)),
        spectral_mode=rec.spectral_mode,
        measured_equivalent_damage=(
            str(q(measured_damage, QTY)) if measured_damage is not None else None
        ),
        planned_equivalent_damage=(
            str(q(planned_damage, QTY)) if planned_damage is not None else None
        ),
        delta_equivalent_damage=delta_damage,
        daily=daily,
    )


def _build_exhibit_reconcile(
    exhibit: Exhibit,
    pairs: list[tuple[_PlacementReconcile, PlacementResult]],
    issues: list[Issue],
    risk_threshold: Decimal,
) -> ExhibitReconcileResult:
    measured_lux = sum((rec.lux_hours for rec, _ in pairs), Decimal(0))
    planned_lux = sum((planned.lux_hours for _, planned in pairs), Decimal(0))
    covered = sum((rec.covered_hours for rec, _ in pairs), Decimal(0))
    elapsed = sum((rec.elapsed_hours for rec, _ in pairs), Decimal(0))
    coverage = covered / elapsed if elapsed > 0 else None

    total_measured = exhibit.historical_dose_lux_hours + measured_lux
    limit = exhibit.dose_limit_lux_hours
    remaining = limit - total_measured
    occupancy = total_measured / limit if limit > 0 else Decimal(0)
    over_by = max(Decimal(0), -remaining)

    measured_damages = [rec.equivalent_damage for rec, _ in pairs
                        if rec.equivalent_damage is not None]
    planned_damages = [planned.equivalent_damage for _, planned in pairs
                       if planned.equivalent_damage is not None]
    measured_damage = sum(measured_damages, Decimal(0)) if measured_damages else None
    planned_damage = sum(planned_damages, Decimal(0)) if planned_damages else None
    spectral_mode = bool(exhibit.sensitivity is not None) and (
        measured_damage is not None or planned_damage is not None
    )

    result_kwargs = dict(
        exhibit_id=exhibit.id,
        spectral_mode=spectral_mode,
        historical_dose_lux_hours=str(q(exhibit.historical_dose_lux_hours, QTY)),
        planned_dose_lux_hours=str(q(planned_lux, QTY)),
        measured_lux_hours=str(q(measured_lux, QTY)),
        delta_lux_hours=str(q(measured_lux - planned_lux, QTY)),
        total_measured_lux_hours=str(q(total_measured, QTY)),
        dose_limit_lux_hours=str(q(limit, QTY)),
        corrected_remaining_lux_hours=str(q(remaining, QTY)),
        coverage_ratio=str(q(coverage, RATIO)) if coverage is not None else None,
        measured_occupancy_ratio=str(q(occupancy, RATIO)),
        over_limit_by_lux_hours=str(q(over_by, QTY)),
        placements=[
            _placement_reconcile_model(rec, planned) for rec, planned in pairs
        ],
    )

    if spectral_mode and exhibit.equivalent_damage_limit is not None:
        damage_limit = exhibit.equivalent_damage_limit
        result_kwargs["historical_equivalent_damage"] = str(
            q(exhibit.historical_equivalent_damage, QTY))
        result_kwargs["equivalent_damage_limit"] = str(q(damage_limit, QTY))
        if planned_damage is not None:
            result_kwargs["planned_equivalent_damage"] = str(q(planned_damage, QTY))
        if measured_damage is not None and planned_damage is not None:
            result_kwargs["delta_equivalent_damage"] = str(
                q(measured_damage - planned_damage, QTY))
        if measured_damage is not None:
            total_damage = exhibit.historical_equivalent_damage + measured_damage
            remaining_damage = damage_limit - total_damage
            damage_ratio = (
                total_damage / damage_limit if damage_limit > 0 else Decimal(0)
            )
            over_damage = max(Decimal(0), -remaining_damage)
            result_kwargs.update(
                measured_equivalent_damage=str(q(measured_damage, QTY)),
                total_measured_equivalent_damage=str(q(total_damage, QTY)),
                corrected_remaining_equivalent_damage=str(q(remaining_damage, QTY)),
                measured_equivalent_damage_ratio=str(q(damage_ratio, RATIO)),
                over_limit_by_equivalent_damage=str(q(over_damage, QTY)),
            )
            if over_damage > 0:
                issues.append(make_issue(
                    "equivalent_damage_limit_exceeded", Severity.ERROR,
                    f"exhibit '{exhibit.id}' measured equivalent damage exceeds "
                    f"its limit by {q(over_damage, QTY)}",
                    {"exhibit_id": exhibit.id},
                    blocking_constraint="equivalent_damage_limit",
                    value=str(q(total_damage, QTY)),
                    limit=str(q(damage_limit, QTY)),
                ))
            elif damage_ratio >= risk_threshold:
                issues.append(make_issue(
                    "equivalent_damage_risk", Severity.RISK,
                    f"exhibit '{exhibit.id}' measured equivalent damage uses "
                    f"{q(damage_ratio * 100, QTY)}% of its limit",
                    {"exhibit_id": exhibit.id},
                    value=str(q(damage_ratio, RATIO)),
                    limit=str(risk_threshold),
                ))

    if over_by > 0:
        issues.append(make_issue(
            "dose_limit_exceeded", Severity.ERROR,
            f"exhibit '{exhibit.id}' measured dose exceeds its lux-hour limit "
            f"by {q(over_by, QTY)}",
            {"exhibit_id": exhibit.id},
            blocking_constraint="dose_limit_lux_hours",
            value=str(q(total_measured, QTY)),
            limit=str(q(limit, QTY)),
        ))
    elif occupancy >= risk_threshold:
        issues.append(make_issue(
            "dose_occupancy_risk", Severity.RISK,
            f"exhibit '{exhibit.id}' measured dose uses "
            f"{q(occupancy * 100, QTY)}% of its lux-hour limit",
            {"exhibit_id": exhibit.id},
            value=str(q(occupancy, RATIO)),
            limit=str(risk_threshold),
        ))

    return ExhibitReconcileResult(**result_kwargs)


def reconcile_exposure(request: ExposureReconcileRequest) -> dict:
    gallery_by_id, exhibit_by_id, issues = validate_references(
        request.placements, request.galleries, request.exhibits
    )

    # Group readings per gallery; request validation already guarantees
    # strictly increasing timestamps within each gallery.
    readings_by_gallery: dict[str, list[_Reading]] = {}
    unknown_reading_galleries: set[str] = set()
    for reading in request.readings:
        if reading.gallery_id not in gallery_by_id:
            unknown_reading_galleries.add(reading.gallery_id)
            continue
        readings_by_gallery.setdefault(reading.gallery_id, []).append(
            _Reading(at=_as_naive_utc(reading.timestamp), lux=reading.lux)
        )
    for gallery_id in sorted(unknown_reading_galleries):
        issues.append(make_issue(
            "unknown_gallery", Severity.ERROR,
            f"readings reference unknown gallery '{gallery_id}'",
            {"gallery_id": gallery_id},
            blocking_constraint="gallery_reference",
        ))

    evaluator = SpectralEvaluator()
    per_exhibit: dict[str, list[tuple[_PlacementReconcile, PlacementResult]]] = {}
    valid_placements: list[Placement] = []
    for index, placement in enumerate(request.placements):
        gallery = gallery_by_id.get(placement.gallery_id)
        exhibit = exhibit_by_id.get(placement.exhibit_id)
        if gallery is None or exhibit is None:
            continue
        valid_placements.append(placement)
        planned = compute_placement(
            placement, gallery, exhibit, evaluator, issues,
            issue_prefix=f"placement:{index}",
        )
        measured = _integrate_placement(
            placement, index, gallery, exhibit,
            readings_by_gallery.get(placement.gallery_id, []),
            max_gap_hours=request.max_sampling_gap_hours,
            evaluator=evaluator,
            issues=issues,
        )
        per_exhibit.setdefault(exhibit.id, []).append((measured, planned))

    # Readings in a known gallery that no valid placement covers.
    placements_by_gallery: dict[str, list[Placement]] = {}
    for placement in valid_placements:
        placements_by_gallery.setdefault(placement.gallery_id, []).append(placement)
    unassigned: dict[tuple[str, date], list[datetime]] = {}
    for reading in request.readings:
        gallery = gallery_by_id.get(reading.gallery_id)
        if gallery is None:
            continue
        at = _as_naive_utc(reading.timestamp)
        covered = any(
            _as_naive_utc(placement.start) <= at <= _as_naive_utc(placement.end)
            for placement in placements_by_gallery.get(reading.gallery_id, [])
        )
        if not covered:
            tz, _ = gallery_timezone(gallery)
            local_day = at.replace(tzinfo=timezone.utc).astimezone(tz).date()
            unassigned.setdefault((reading.gallery_id, local_day), []).append(at)
    for (gallery_id, day), stamps in sorted(unassigned.items()):
        issues.append(make_issue(
            "unassigned_reading", Severity.RISK,
            f"{len(stamps)} reading(s) in gallery '{gallery_id}' on "
            f"{day.isoformat()} fall outside every placement and cannot be "
            "attributed to an exhibit",
            {"gallery_id": gallery_id, "date": day.isoformat(),
             "count": len(stamps),
             "first_timestamp": stamps[0].isoformat(),
             "last_timestamp": stamps[-1].isoformat()},
        ))

    exhibit_results = [
        _build_exhibit_reconcile(
            exhibit, per_exhibit.get(exhibit.id, []), issues, request.risk_threshold
        )
        for exhibit in request.exhibits
    ]

    has_error = any(issue.severity == Severity.ERROR for issue in issues)
    incomplete = any(issue.code in INCOMPLETE_CODES for issue in issues)
    if has_error:
        status = Status.INFEASIBLE
    elif incomplete:
        status = Status.INCOMPLETE
    else:
        status = Status.FEASIBLE

    return {
        "kind": "exposure_reconcile",
        "status": status,
        "horizon": TimeInterval(start=request.horizon_start, end=request.horizon_end),
        "issues": issues,
        "exhibits": exhibit_results,
    }
