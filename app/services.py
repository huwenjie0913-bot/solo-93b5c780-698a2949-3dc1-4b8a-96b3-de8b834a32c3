"""Request-level dose aggregation and validation."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .engine import (
    QTY,
    RATIO,
    compute_placement,
    make_issue,
    placement_result_to_model,
    q,
)
from .models import (
    DoseComputeRequest,
    Exhibit,
    ExhibitDoseResult,
    Gallery,
    Issue,
    Placement,
    Severity,
    Status,
    TimeInterval,
)
from .spectrum import SpectralEvaluator


def index_galleries(galleries: list[Gallery]) -> dict[str, Gallery]:
    return {gallery.id: gallery for gallery in galleries}


def validate_references(
    placements: list[Placement],
    galleries: list[Gallery],
    exhibits: list[Exhibit],
) -> tuple[dict[str, Gallery], dict[str, Exhibit], list[Issue]]:
    issues: list[Issue] = []
    gallery_by_id = index_galleries(galleries)
    exhibit_by_id = {exhibit.id: exhibit for exhibit in exhibits}

    if len(gallery_by_id) != len(galleries):
        duplicates = sorted({g.id for g in galleries
                             if sum(1 for x in galleries if x.id == g.id) > 1})
        issues.append(make_issue(
            "duplicate_gallery_id", Severity.ERROR,
            f"duplicate gallery identifiers: {', '.join(duplicates)}",
            {"gallery_ids": duplicates},
        ))
    if len(exhibit_by_id) != len(exhibits):
        seen: set[str] = set()
        duplicates = []
        for exhibit in exhibits:
            if exhibit.id in seen:
                duplicates.append(exhibit.id)
            seen.add(exhibit.id)
        issues.append(make_issue(
            "duplicate_exhibit_id", Severity.ERROR,
            f"duplicate exhibit identifiers: {', '.join(sorted(duplicates))}",
            {"exhibit_ids": sorted(duplicates)},
        ))

    for index, placement in enumerate(placements):
        if placement.gallery_id not in gallery_by_id:
            issues.append(make_issue(
                "unknown_gallery", Severity.ERROR,
                f"placement {index} references unknown gallery '{placement.gallery_id}'",
                {"placement_index": index, "gallery_id": placement.gallery_id},
                blocking_constraint="gallery_reference",
            ))
        if placement.exhibit_id not in exhibit_by_id:
            issues.append(make_issue(
                "unknown_exhibit", Severity.ERROR,
                f"placement {index} references unknown exhibit '{placement.exhibit_id}'",
                {"placement_index": index, "exhibit_id": placement.exhibit_id},
                blocking_constraint="exhibit_reference",
            ))
    return gallery_by_id, exhibit_by_id, issues


def _sorted_disjoint_gap(intervals: list[tuple[datetime, datetime]], minimum_hours: Decimal):
    """Return the largest gap (in hours) between disjoint intervals, else None."""
    if not intervals or minimum_hours <= 0:
        return None
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    largest_gap = Decimal(0)
    for (_, previous_end), (next_start, _) in zip(merged, merged[1:]):
        gap_hours = Decimal((next_start - previous_end).total_seconds()) / Decimal(3600)
        largest_gap = max(largest_gap, gap_hours)
    return largest_gap if largest_gap > 0 else None


def build_exhibit_result(
    exhibit: Exhibit,
    placement_results: list,
    issues: list[Issue],
    risk_threshold: Decimal,
) -> ExhibitDoseResult:
    planned_lux_hours = sum(
        (placement.lux_hours for placement in placement_results), Decimal(0)
    )
    spectral_doses = [
        placement.equivalent_damage
        for placement in placement_results
        if placement.equivalent_damage is not None
    ]
    spectral_mode = bool(exhibit.sensitivity is not None) and bool(spectral_doses)
    planned_damage = sum(spectral_doses, Decimal(0)) if spectral_doses else None

    total_lux_hours = exhibit.historical_dose_lux_hours + planned_lux_hours
    occupancy = (
        total_lux_hours / exhibit.dose_limit_lux_hours
        if exhibit.dose_limit_lux_hours > 0 else Decimal(0)
    )
    remaining_lux_hours = exhibit.dose_limit_lux_hours - total_lux_hours
    over_by_lux = max(Decimal(0), -remaining_lux_hours)

    elapsed_hours = sum(
        (placement.elapsed_hours for placement in placement_results), Decimal(0)
    )

    intervals = [
        (placement.placement.start, placement.placement.end)
        for placement in placement_results
    ]
    minimum_rest_gap = _sorted_disjoint_gap(intervals, exhibit.minimum_rest_hours)

    result_kwargs = dict(
        exhibit_id=exhibit.id,
        spectral_mode=spectral_mode,
        historical_dose_lux_hours=str(q(exhibit.historical_dose_lux_hours, QTY)),
        planned_dose_lux_hours=str(q(planned_lux_hours, QTY)),
        total_dose_lux_hours=str(q(total_lux_hours, QTY)),
        dose_limit_lux_hours=str(q(exhibit.dose_limit_lux_hours, QTY)),
        remaining_lux_hours=str(q(remaining_lux_hours, QTY)),
        occupancy_ratio=str(q(occupancy, RATIO)),
        remaining_ratio=str(q(max(Decimal(0), Decimal(1) - occupancy), RATIO)),
        over_limit_by_lux_hours=str(q(over_by_lux, QTY)),
        placements=[
            placement_result_to_model(placement, index)
            for index, placement in enumerate(placement_results)
        ],
        minimum_display_elapsed_hours=str(q(exhibit.minimum_display_hours)),
        actual_display_elapsed_hours=str(q(elapsed_hours)),
        minimum_rest_hours=str(q(exhibit.minimum_rest_hours)),
        minimum_rest_gap_hours=(
            str(q(minimum_rest_gap)) if minimum_rest_gap is not None else None
        ),
    )

    if spectral_mode and exhibit.equivalent_damage_limit is not None:
        historical_damage = exhibit.historical_equivalent_damage
        total_damage = historical_damage + planned_damage
        limit_damage = exhibit.equivalent_damage_limit
        remaining_damage = limit_damage - total_damage
        damage_ratio = total_damage / limit_damage if limit_damage > 0 else Decimal(0)
        over_by_damage = max(Decimal(0), -remaining_damage)
        result_kwargs.update(
            historical_equivalent_damage=str(q(historical_damage, QTY)),
            planned_equivalent_damage=str(q(planned_damage, QTY)),
            total_equivalent_damage=str(q(total_damage, QTY)),
            equivalent_damage_limit=str(q(limit_damage, QTY)),
            remaining_equivalent_damage=str(q(remaining_damage, QTY)),
            equivalent_damage_ratio=str(q(damage_ratio, RATIO)),
            equivalent_damage_remaining_ratio=str(
                q(max(Decimal(0), Decimal(1) - damage_ratio), RATIO)),
            over_limit_by_equivalent_damage=str(q(over_by_damage, QTY)),
        )
        if over_by_damage > 0:
            issues.append(make_issue(
                "equivalent_damage_limit_exceeded", Severity.ERROR,
                f"exhibit '{exhibit.id}' exceeds its equivalent-damage limit by "
                f"{q(over_by_damage, QTY)}",
                {"exhibit_id": exhibit.id},
                blocking_constraint="equivalent_damage_limit",
                value=str(q(total_damage, QTY)),
                limit=str(q(limit_damage, QTY)),
            ))
        elif damage_ratio >= risk_threshold:
            issues.append(make_issue(
                "equivalent_damage_risk", Severity.RISK,
                f"exhibit '{exhibit.id}' uses {q(damage_ratio * 100, QTY)}% of its "
                f"equivalent-damage limit",
                {"exhibit_id": exhibit.id},
                value=str(q(damage_ratio, RATIO)),
                limit=str(risk_threshold),
            ))

    if over_by_lux > 0:
        issues.append(make_issue(
            "dose_limit_exceeded", Severity.ERROR,
            f"exhibit '{exhibit.id}' exceeds its lux-hour limit by {q(over_by_lux, QTY)}",
            {"exhibit_id": exhibit.id},
            blocking_constraint="dose_limit_lux_hours",
            value=str(q(total_lux_hours, QTY)),
            limit=str(q(exhibit.dose_limit_lux_hours, QTY)),
        ))
    elif occupancy >= risk_threshold:
        issues.append(make_issue(
            "dose_occupancy_risk", Severity.RISK,
            f"exhibit '{exhibit.id}' uses {q(occupancy * 100, QTY)}% of its lux-hour limit",
            {"exhibit_id": exhibit.id},
            value=str(q(occupancy, RATIO)),
            limit=str(risk_threshold),
        ))

    if elapsed_hours < exhibit.minimum_display_hours:
        issues.append(make_issue(
            "minimum_display_not_met", Severity.ERROR,
            f"exhibit '{exhibit.id}' is displayed {q(elapsed_hours)}h but requires at "
            f"least {q(exhibit.minimum_display_hours)}h",
            {"exhibit_id": exhibit.id},
            blocking_constraint="minimum_display_hours",
            value=str(q(elapsed_hours)),
            limit=str(q(exhibit.minimum_display_hours)),
        ))
    if (
        exhibit.minimum_rest_hours > 0
        and minimum_rest_gap is not None
        and minimum_rest_gap < exhibit.minimum_rest_hours
    ):
        issues.append(make_issue(
            "minimum_rest_not_met", Severity.RISK,
            f"exhibit '{exhibit.id}' has a {q(minimum_rest_gap)}h rest gap but requires "
            f"{q(exhibit.minimum_rest_hours)}h",
            {"exhibit_id": exhibit.id},
            value=str(q(minimum_rest_gap)),
            limit=str(q(exhibit.minimum_rest_hours)),
        ))

    return ExhibitDoseResult(**result_kwargs)


def evaluate_placements(
    placements: list[Placement],
    gallery_by_id: dict[str, Gallery],
    exhibit_by_id: dict[str, Exhibit],
    *,
    clamp_to_horizon: tuple[datetime, datetime] | None = None,
) -> tuple[list, list[Issue]]:
    """Run the engine for every valid placement reference.

    Returns per-placement internal results (aligned with valid placements)
    and spectrum/calendar issues.
    """
    evaluator = SpectralEvaluator()
    issues: list[Issue] = []
    results = []
    horizon_start, horizon_end = clamp_to_horizon or (None, None)

    for index, placement in enumerate(placements):
        gallery = gallery_by_id.get(placement.gallery_id)
        exhibit = exhibit_by_id.get(placement.exhibit_id)
        if gallery is None or exhibit is None:
            continue
        if clamp_to_horizon is not None:
            if placement.end <= horizon_start or placement.start >= horizon_end:
                continue
        result = compute_placement(
            placement, gallery, exhibit, evaluator, issues,
            issue_prefix=f"placement:{index}",
        )
        results.append(result)
    return results, issues


def compute_dose(request: DoseComputeRequest) -> dict:
    gallery_by_id, exhibit_by_id, ref_issues = validate_references(
        request.placements, request.galleries, request.exhibits
    )
    placement_results, spectral_issues = evaluate_placements(
        request.placements, gallery_by_id, exhibit_by_id
    )
    issues = ref_issues + spectral_issues

    by_exhibit: dict[str, list] = {}
    for placement, result in zip(
        [p for p in request.placements
         if p.gallery_id in gallery_by_id and p.exhibit_id in exhibit_by_id],
        placement_results,
    ):
        by_exhibit.setdefault(placement.exhibit_id, []).append(result)

    exhibit_results = []
    for exhibit in request.exhibits:
        exhibit_results.append(build_exhibit_result(
            exhibit,
            by_exhibit.get(exhibit.id, []),
            issues,
            request.risk_threshold,
        ))

    has_error = any(issue.severity == Severity.ERROR for issue in issues)
    status = Status.INFEASIBLE if has_error else Status.FEASIBLE

    return {
        "kind": "dose",
        "status": status,
        "horizon": TimeInterval(start=request.horizon_start, end=request.horizon_end),
        "issues": issues,
        "exhibits": exhibit_results,
    }
