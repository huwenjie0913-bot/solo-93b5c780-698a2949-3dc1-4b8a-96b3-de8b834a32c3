"""Calendar and light-dose engine.

Computes, for each exhibit placement, its overlap with the gallery's
illumination segments (split per local calendar day), the resulting
lux-hours and — when both the source spectrum and the material sensitivity
curve are present — the equivalent damage via the spectral action integral.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .models import (
    DailyContribution,
    Exhibit,
    Gallery,
    IlluminationSegment,
    Issue,
    Placement,
    PlacementDoseResult,
    SegmentContribution,
    Severity,
    BandContribution,
)
from .spectrum import (
    BandAggregate,
    SpectralEvaluation,
    SpectralEvaluator,
    SpectralFailure,
)

HOURS = Decimal("0.0001")
QTY = Decimal("0.000001")
RATIO = Decimal("0.000001")
SECONDS_PER_HOUR = Decimal(3600)


def q(value: Decimal, places: Decimal = HOURS) -> Decimal:
    return Decimal(value).quantize(places)


def hours_between(start: datetime, end: datetime) -> Decimal:
    return Decimal((end - start).total_seconds()) / SECONDS_PER_HOUR


def make_issue(
    code: str,
    severity: Severity,
    message: str,
    location: dict | None = None,
    *,
    blocking_constraint: str | None = None,
    value: str | None = None,
    limit: str | None = None,
) -> Issue:
    return Issue(
        code=code,
        severity=severity,
        message=message,
        location=location or {},
        blocking_constraint=blocking_constraint,
        value=value,
        limit=limit,
    )


def gallery_timezone(gallery: Gallery) -> tuple[ZoneInfo | timezone, bool]:
    """Return (timezone, valid). Invalid timezone names fall back to UTC."""
    try:
        return ZoneInfo(gallery.timezone), True
    except Exception:
        return timezone.utc, False


@dataclass
class SegmentResult:
    start: datetime
    end: datetime
    overlap_start: datetime
    overlap_end: datetime
    lux: Decimal
    elapsed_hours: Decimal
    lux_hours: Decimal
    factor: Decimal | None = None  # None => classic lux-hour accounting
    equivalent_damage: Decimal | None = None
    bands: list[BandContribution] = field(default_factory=list)
    spectrum_present: bool = False


@dataclass
class DayResult:
    day: date
    day_start_utc: datetime
    day_end_utc: datetime
    open_hours: Decimal
    segments: list[SegmentResult] = field(default_factory=list)

    @property
    def illuminated_hours(self) -> Decimal:
        return sum((segment.elapsed_hours for segment in self.segments), Decimal(0))

    @property
    def lux_hours(self) -> Decimal:
        return sum((segment.lux_hours for segment in self.segments), Decimal(0))

    @property
    def equivalent_damage(self) -> Decimal | None:
        damages = [s.equivalent_damage for s in self.segments if s.equivalent_damage is not None]
        if not damages:
            return None
        return sum(damages, Decimal(0))


@dataclass
class PlacementResult:
    placement: Placement
    gallery: Gallery
    days: list[DayResult] = field(default_factory=list)
    elapsed_hours: Decimal = Decimal(0)
    open_hours: Decimal = Decimal(0)
    illuminated_hours: Decimal = Decimal(0)
    lux_hours: Decimal = Decimal(0)
    equivalent_damage: Decimal | None = None
    bands: dict[str, BandAggregate] = field(default_factory=dict)
    spectral_mode: bool = False


def _parse_hhmm(value: str) -> time:
    parts = value.split(":")
    if len(parts) == 2:
        return time(int(parts[0]), int(parts[1]))
    return time(int(parts[0]), int(parts[1]), int(parts[2]))


def open_intervals_for_day(gallery: Gallery, day: date, tz: ZoneInfo | timezone):
    """Return list of open (start_utc, end_utc) intervals for a local day."""
    intervals = []
    for weekly in gallery.weekly_open:
        if weekly.weekday != day.weekday():
            continue
        local_start = datetime.combine(day, _parse_hhmm(weekly.open_time), tzinfo=tz)
        local_end = datetime.combine(day, _parse_hhmm(weekly.close_time), tzinfo=tz)
        start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
        end = local_end.astimezone(timezone.utc).replace(tzinfo=None)
        if end > start:
            intervals.append((start, end))
        else:  # overnight opening: split at local midnight, second half lands next day
            intervals.append((start, datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
                              .astimezone(timezone.utc).replace(tzinfo=None)))
            intervals.append((datetime.combine(day, time.min, tzinfo=tz)
                              .astimezone(timezone.utc).replace(tzinfo=None), end))
    # Remove closures caused by closed exceptions with expected_open=True.
    closed = []
    for exception in gallery.closed_exceptions:
        if exception.expected_open:
            closed.append((_as_naive_utc(exception.start), _as_naive_utc(exception.end)))
    if closed:
        cut = []
        for start, end in intervals:
            pieces = [(start, end)]
            for close_start, close_end in closed:
                next_pieces = []
                for piece_start, piece_end in pieces:
                    overlap_start = max(piece_start, close_start)
                    overlap_end = min(piece_end, close_end)
                    if overlap_end <= overlap_start:
                        next_pieces.append((piece_start, piece_end))
                    else:
                        if piece_start < overlap_start:
                            next_pieces.append((piece_start, overlap_start))
                        if overlap_end < piece_end:
                            next_pieces.append((overlap_end, piece_end))
                pieces = next_pieces
            cut.extend(pieces)
        intervals = cut
    return sorted(intervals)


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def local_days_between(
    start: datetime, end: datetime, tz: ZoneInfo | timezone
) -> list[tuple[date, datetime, datetime]]:
    """Yield (local date, naive-UTC day start, naive-UTC day end) covering [start, end)."""
    start_utc = _as_naive_utc(start)
    end_utc = _as_naive_utc(end)
    first_local_date = start_utc.replace(tzinfo=timezone.utc).astimezone(tz).date()
    last_local_date = (end_utc - timedelta(microseconds=1)).replace(tzinfo=timezone.utc).astimezone(tz).date()
    days = []
    cursor = first_local_date
    while cursor <= last_local_date:
        day_start = datetime.combine(cursor, time.min, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
        day_end = datetime.combine(cursor + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
        days.append((cursor, day_start, day_end))
        cursor += timedelta(days=1)
    return days


def compute_placement(
    placement: Placement,
    gallery: Gallery,
    exhibit: Exhibit,
    evaluator: SpectralEvaluator,
    issues: list[Issue],
    *,
    issue_prefix: str,
) -> PlacementResult:
    """Integrate illumination over one placement, emitting issues in place."""
    tz, tz_valid = gallery_timezone(gallery)
    if not tz_valid:
        issues.append(make_issue(
            "invalid_timezone", Severity.RISK,
            f"gallery '{gallery.id}' has unknown timezone '{gallery.timezone}'; using UTC",
            {"gallery_id": gallery.id},
        ))

    placement_start = _as_naive_utc(placement.start)
    placement_end = _as_naive_utc(placement.end)
    result = PlacementResult(placement=placement, gallery=gallery)
    result.elapsed_hours = hours_between(placement_start, placement_end)

    # placement-local issue dedupe so multiple segments report a gap once
    reported_gaps: set[str] = set()

    for day, day_start, day_end in local_days_between(placement_start, placement_end, tz):
        open_intervals = open_intervals_for_day(gallery, day, tz)
        open_hours = sum(
            (hours_between(start, end) for start, end in open_intervals), Decimal(0)
        )
        day_result = DayResult(day=day, day_start_utc=day_start, day_end_utc=day_end,
                               open_hours=open_hours)

        for segment in gallery.illumination:
            seg_start = _as_naive_utc(segment.start)
            seg_end = _as_naive_utc(segment.end)
            overlap_start = max(seg_start, placement_start, day_start)
            overlap_end = min(seg_end, placement_end, day_end)
            if overlap_end <= overlap_start:
                continue

            elapsed = hours_between(overlap_start, overlap_end)
            lux_hours = segment.lux * elapsed
            segment_result = SegmentResult(
                start=seg_start, end=seg_end,
                overlap_start=overlap_start, overlap_end=overlap_end,
                lux=segment.lux,
                elapsed_hours=elapsed, lux_hours=lux_hours,
                spectrum_present=segment.spectrum is not None,
            )

            if exhibit.sensitivity is not None and segment.spectrum is not None:
                outcome = evaluator.evaluate(segment, exhibit)
                if isinstance(outcome, SpectralEvaluation):
                    factor = Decimal(str(outcome.factor))
                    equivalent_damage = factor * lux_hours
                    segment_result.factor = factor
                    segment_result.equivalent_damage = equivalent_damage
                    segment_result.bands = _build_band_contributions(
                        outcome.bands(), lux_hours, factor
                    )
                    if outcome.missing_ranges:
                        for gap in outcome.missing_ranges:
                            key = f"{gap.start_nm:g}-{gap.end_nm:g}-{gap.missing}"
                            if key in reported_gaps:
                                continue
                            reported_gaps.add(key)
                            issues.append(_coverage_gap_issue(
                                outcome, gap, gallery, exhibit, segment, issue_prefix
                            ))
                    if min(outcome.light_coverage, outcome.sensitivity_coverage) < 0.999:
                        issues.append(make_issue(
                            "spectrum_partial_coverage", Severity.RISK,
                            (f"equivalent damage for exhibit '{exhibit.id}' integrates only "
                             f"{outcome.light_coverage:.1%} of source power and "
                             f"{outcome.sensitivity_coverage:.1%} of sensitivity weight "
                             f"over {outcome.common_start_nm:g}-{outcome.common_end_nm:g} nm"),
                            {"exhibit_id": exhibit.id, "gallery_id": gallery.id,
                             "common_wavelength_range_nm": [
                                 f"{outcome.common_start_nm:g}", f"{outcome.common_end_nm:g}"]},
                        ))
                else:
                    issues.append(_spectral_failure_issue(
                        outcome, gallery, exhibit, segment, issue_prefix))
            elif exhibit.sensitivity is not None or segment.spectrum is not None:
                code = (
                    "sensitivity_missing"
                    if segment.spectrum is not None
                    else "source_spectrum_missing"
                )
                if code not in reported_gaps:
                    reported_gaps.add(code)
                    issues.append(make_issue(
                        code, Severity.RISK,
                        (f"segment in gallery '{gallery.id}' carries a source spectrum but "
                         f"exhibit '{exhibit.id}' has no sensitivity curve; using lux-hours"
                         if code == "sensitivity_missing"
                         else f"exhibit '{exhibit.id}' has a sensitivity curve but the "
                              f"illuminated segment in gallery '{gallery.id}' has no source "
                              f"spectrum; using lux-hours"),
                        {"exhibit_id": exhibit.id, "gallery_id": gallery.id},
                    ))

            day_result.segments.append(segment_result)

        if day_result.segments:
            result.days.append(day_result)

    # Totals
    result.open_hours = sum((day.open_hours for day in result.days), Decimal(0))
    result.illuminated_hours = sum(
        (day.illuminated_hours for day in result.days), Decimal(0)
    )
    result.lux_hours = sum((day.lux_hours for day in result.days), Decimal(0))
    damages = [
        segment.equivalent_damage
        for day in result.days
        for segment in day.segments
        if segment.equivalent_damage is not None
    ]
    if damages:
        result.equivalent_damage = sum(damages, Decimal(0))
        result.spectral_mode = True
        result.bands = _aggregate_placement_bands(result)
    return result


def _build_band_contributions(
    bands: list[BandAggregate], lux_hours: Decimal, factor: Decimal
) -> list[BandContribution]:
    contributions = []
    for aggregate in bands:
        band_damage = Decimal(str(aggregate.damage)) * lux_hours
        fraction = (
            Decimal(str(aggregate.damage)) / factor if factor != 0 else Decimal(0)
        )
        contributions.append(BandContribution(
            wavelength_start_nm=f"{aggregate.start_nm:g}",
            wavelength_end_nm=f"{aggregate.end_nm:g}",
            band_name=aggregate.name,
            equivalent_damage=str(q(band_damage, QTY)),
            equivalent_damage_fraction=str(q(fraction, RATIO)),
        ))
    return contributions


def _aggregate_placement_bands(result: PlacementResult) -> dict[str, BandAggregate]:
    merged: dict[str, BandAggregate] = {}
    for day in result.days:
        for segment in day.segments:
            for band in segment.bands:
                name = band.band_name or f"{band.wavelength_start_nm}-{band.wavelength_end_nm}"
                damage = Decimal(band.equivalent_damage)
                start_nm = float(band.wavelength_start_nm)
                end_nm = float(band.wavelength_end_nm)
                aggregate = merged.get(name)
                if aggregate is None:
                    merged[name] = BandAggregate(name, start_nm, end_nm, float(damage))
                else:
                    aggregate.damage += float(damage)
                    aggregate.start_nm = min(aggregate.start_nm, start_nm)
                    aggregate.end_nm = max(aggregate.end_nm, end_nm)
    return merged


def _coverage_gap_issue(
    outcome: SpectralEvaluation,
    gap,
    gallery: Gallery,
    exhibit: Exhibit,
    segment: IlluminationSegment,
    issue_prefix: str,
) -> Issue:
    return make_issue(
        "spectrum_coverage_gap", Severity.RISK,
        (f"wavelength range {gap.start_nm:g}-{gap.end_nm} nm covered only by the "
         f"{'source spectrum' if gap.missing == 'sensitivity' else 'sensitivity curve'} "
         f"is excluded from the equivalent-damage integral for exhibit '{exhibit.id}'"),
        {
            "exhibit_id": exhibit.id,
            "gallery_id": gallery.id,
            "missing_wavelength_ranges_nm": [
                {"start_nm": f"{gap.start_nm:g}", "end_nm": f"{gap.end_nm:g}",
                 "missing_curve": gap.missing}
            ],
            "common_wavelength_range_nm": [
                f"{outcome.common_start_nm:g}", f"{outcome.common_end_nm:g}"],
            "reference": issue_prefix,
        },
    )


def _spectral_failure_issue(
    failure: SpectralFailure,
    gallery: Gallery,
    exhibit: Exhibit,
    segment: IlluminationSegment,
    issue_prefix: str,
) -> Issue:
    ranges = [
        {"start_nm": f"{gap.start_nm:g}", "end_nm": f"{gap.end_nm:g}",
         "missing_curve": gap.missing}
        for gap in failure.missing_ranges
    ]
    severity = Severity.ERROR if failure.code == "spectrum_no_overlap" else Severity.RISK
    return make_issue(
        failure.code, severity,
        f"{failure.message} for exhibit '{exhibit.id}' in gallery '{gallery.id}'",
        {"exhibit_id": exhibit.id, "gallery_id": gallery.id,
         "missing_wavelength_ranges_nm": ranges, "reference": issue_prefix},
        blocking_constraint="spectral_action_function" if severity == Severity.ERROR else None,
    )


def placement_result_to_model(
    result: PlacementResult, placement_index: int
) -> PlacementDoseResult:
    daily = []
    for day in result.days:
        segments = []
        for segment in day.segments:
            segments.append(SegmentContribution(
                segment_start=segment.start,
                segment_end=segment.end,
                overlap_start=segment.overlap_start,
                overlap_end=segment.overlap_end,
                lux=str(segment.lux),
                elapsed_hours=str(q(segment.elapsed_hours)),
                lux_hours=str(q(segment.lux_hours, QTY)),
                spectrum_present=segment.spectrum_present,
                damage_factor=str(q(segment.factor or Decimal(0), RATIO))
                if segment.factor is not None else None,
                equivalent_damage=str(q(segment.equivalent_damage, QTY))
                if segment.equivalent_damage is not None else None,
                bands=segment.bands,
            ))
        daily.append(DailyContribution(
            gallery_date=day.day.isoformat(),
            local_day_start=day.day_start_utc,
            local_day_end=day.day_end_utc,
            open_hours=str(q(day.open_hours)),
            illuminated_hours=str(q(day.illuminated_hours)),
            lux_hours=str(q(day.lux_hours, QTY)),
            equivalent_damage=str(q(day.equivalent_damage, QTY))
            if day.equivalent_damage is not None else None,
            segments=segments,
        ))
    return PlacementDoseResult(
        placement_index=placement_index,
        placement_id=result.placement.id,
        exhibit_id=result.placement.exhibit_id,
        gallery_id=result.placement.gallery_id,
        start=result.placement.start,
        end=result.placement.end,
        elapsed_hours=str(q(result.elapsed_hours)),
        open_hours=str(q(result.open_hours)),
        illuminated_hours=str(q(result.illuminated_hours)),
        lux_hours=str(q(result.lux_hours, QTY)),
        spectral_mode=result.spectral_mode,
        equivalent_damage=str(q(result.equivalent_damage, QTY))
        if result.equivalent_damage is not None else None,
        bands=[
            BandContribution(
                wavelength_start_nm=f"{aggregate.start_nm:g}",
                wavelength_end_nm=f"{aggregate.end_nm:g}",
                band_name=aggregate.name,
                equivalent_damage=str(q(Decimal(str(aggregate.damage)), QTY)),
                equivalent_damage_fraction=(
                    str(q(Decimal(str(aggregate.damage)) / result.equivalent_damage, RATIO))
                    if result.equivalent_damage and result.equivalent_damage != 0
                    else "0.000000"
                ),
            )
            for aggregate in result.bands.values()
        ],
        daily=daily,
    )
