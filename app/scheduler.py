"""Rotation search with equivalent-damage margin filtering.

Candidate display blocks are enumerated between change instants for every
(exhibit, gallery) pair. Each candidate is scored with the illumination
engine; in spectral mode the block is admitted only when its equivalent
damage fits inside the exhibit's remaining equivalent-damage allowance
(historical damage subtracted), otherwise it is rejected by the
``equivalent_damage_margin`` constraint. A most-constrained-first greedy
backtracking assignment then honours gallery capacity and minimum rest gaps.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .engine import (
    QTY,
    RATIO,
    compute_placement,
    make_issue,
    q,
)
from .models import (
    Exhibit,
    Gallery,
    Issue,
    Placement,
    RotationRequest,
    ScheduleBlock,
    Severity,
    Status,
    TimeInterval,
)
from .spectrum import SpectralEvaluator
from .services import build_exhibit_result, validate_references


def _naive(value: datetime) -> datetime:
    from .engine import _as_naive_utc
    return _as_naive_utc(value)


def _change_instants(request: RotationRequest) -> list[datetime]:
    instants = sorted({
        _naive(request.horizon_start),
        _naive(request.horizon_end),
        *(_naive(instant) for instant in request.change_dates
          if request.horizon_start <= instant <= request.horizon_end),
    })
    return instants


def _candidate_galleries(exhibit: Exhibit, galleries: list[Gallery]) -> list[Gallery]:
    if exhibit.candidate_gallery_ids:
        wanted = set(exhibit.candidate_gallery_ids)
        return [g for g in galleries if g.id in wanted]
    return list(galleries)


def _rest_gap_hours(intervals: list[tuple[datetime, datetime]]) -> Decimal:
    if len(intervals) < 2:
        return Decimal(0)
    ordered = sorted(intervals)
    return min(
        Decimal((next_start - previous_end).total_seconds()) / Decimal(3600)
        for (_, previous_end), (next_start, _) in zip(ordered, ordered[1:])
    )


def search_rotation(request: RotationRequest) -> dict:
    gallery_by_id, exhibit_by_id, ref_issues = validate_references(
        [], request.galleries, request.exhibits
    )
    issues: list[Issue] = list(ref_issues)

    # Candidate galleries referenced but unknown.
    known_ids = set(gallery_by_id)
    for exhibit in request.exhibits:
        missing = sorted(set(exhibit.candidate_gallery_ids) - known_ids)
        if missing:
            issues.append(make_issue(
                "unknown_candidate_gallery", Severity.ERROR,
                f"exhibit '{exhibit.id}' lists unknown galleries: {', '.join(missing)}",
                {"exhibit_id": exhibit.id, "gallery_ids": missing},
                blocking_constraint="gallery_reference",
            ))

    instants = _change_instants(request)
    evaluator = SpectralEvaluator()

    # ---- Enumerate and margin-filter candidates per exhibit ----------------
    # candidate: dict with block boundaries, gallery, internal engine result
    per_exhibit_candidates: dict[str, list[dict]] = {}
    rejection_details: dict[str, list[Issue]] = {}
    # Per-exhibit counters for margin rejections (full detail stays in `search`).
    margin_rejections: dict[str, int] = {}

    for exhibit in request.exhibits:
        candidates: list[dict] = []
        best_lux: Decimal | None = None
        best_damage: Decimal | None = None
        best_hours: Decimal | None = None

        historical_damage = exhibit.historical_equivalent_damage
        damage_limit = exhibit.equivalent_damage_limit or Decimal(0)
        damage_headroom = damage_limit - historical_damage
        spectral_exhibit = exhibit.sensitivity is not None
        # Classic exhibits are filtered by the lux-hour margin; spectral
        # exhibits by the equivalent-damage margin. Both use an epsilon.
        lux_headroom = exhibit.dose_limit_lux_hours - exhibit.historical_dose_lux_hours
        tolerance = Decimal("0.0000001")

        galleries = _candidate_galleries(exhibit, request.galleries)
        for gallery in galleries:
            for start, end in zip(instants, instants[1:]):
                if end <= start:
                    continue
                placement = Placement(
                    exhibit_id=exhibit.id, gallery_id=gallery.id,
                    start=start, end=end,
                )
                local_issues: list[Issue] = []
                result = compute_placement(
                    placement, gallery, exhibit, evaluator, local_issues,
                    issue_prefix=f"candidate:{exhibit.id}:{gallery.id}",
                )
                # A block the gallery never illuminates is not a display slot:
                # it carries no dose and must not win on a zero-damage sort.
                if result.illuminated_hours <= 0 or result.lux_hours <= 0:
                    continue
                if result.illuminated_hours < exhibit.minimum_display_hours:
                    continue
                # Promote non-blocking spectrum issues once per exhibit/gallery.
                for issue in local_issues:
                    if issue.severity == Severity.ERROR:
                        rejection_details.setdefault(exhibit.id, []).append(issue)

                candidate_damage = result.equivalent_damage
                candidate = {
                    "exhibit_id": exhibit.id,
                    "gallery_id": gallery.id,
                    "start": start,
                    "end": end,
                    "result": result,
                    "duration_hours": result.illuminated_hours,
                }
                lux_hours = result.lux_hours
                if best_lux is None or lux_hours < best_lux:
                    best_lux = lux_hours
                    best_hours = result.illuminated_hours
                    best_damage = candidate_damage

                if spectral_exhibit and candidate_damage is not None:
                    if candidate_damage > damage_headroom + tolerance:
                        margin_rejections[exhibit.id] = (
                            margin_rejections.get(exhibit.id, 0) + 1
                        )
                        rejection_details.setdefault(exhibit.id, []).append(make_issue(
                            "equivalent_damage_margin", Severity.RISK,
                            (f"candidate block {start.isoformat()}..{end.isoformat()} in "
                             f"gallery '{gallery.id}' would use "
                             f"{q(candidate_damage, QTY)} equivalent damage but only "
                             f"{q(max(Decimal(0), damage_headroom), QTY)} remains"),
                            {"exhibit_id": exhibit.id, "gallery_id": gallery.id,
                             "block_start": start.isoformat(), "block_end": end.isoformat()},
                            blocking_constraint="equivalent_damage_limit",
                            value=str(q(candidate_damage, QTY)),
                            limit=str(q(max(Decimal(0), damage_headroom), QTY)),
                        ))
                        continue
                elif not spectral_exhibit:
                    if lux_hours > lux_headroom + tolerance:
                        margin_rejections[exhibit.id] = (
                            margin_rejections.get(exhibit.id, 0) + 1
                        )
                        rejection_details.setdefault(exhibit.id, []).append(make_issue(
                            "lux_hours_margin", Severity.RISK,
                            (f"candidate block {start.isoformat()}..{end.isoformat()} in "
                             f"gallery '{gallery.id}' would use "
                             f"{q(lux_hours, QTY)} lux-hours but only "
                             f"{q(max(Decimal(0), lux_headroom), QTY)} remains"),
                            {"exhibit_id": exhibit.id, "gallery_id": gallery.id,
                             "block_start": start.isoformat(), "block_end": end.isoformat()},
                            blocking_constraint="dose_limit_lux_hours",
                            value=str(q(lux_hours, QTY)),
                            limit=str(q(max(Decimal(0), lux_headroom), QTY)),
                        ))
                        continue
                candidates.append(candidate)

        candidates.sort(key=lambda c: (
            c["result"].equivalent_damage
            if c["result"].equivalent_damage is not None
            else c["result"].lux_hours
        ))
        per_exhibit_candidates[exhibit.id] = candidates
        exhibit._best = (best_lux, best_hours, best_damage)  # type: ignore[attr-defined]

    # Deduplicate issues promoted from candidate evaluation. Margin rejections
    # are summarized: one issue per exhibit with the rejected-block count.
    seen_issue_keys: set[tuple] = set()
    for exhibit_id, detail_list in rejection_details.items():
        margin_count = margin_rejections.get(exhibit_id, 0)
        emitted_margin: set[str] = set()
        for issue in detail_list:
            if issue.code in {"equivalent_damage_margin", "lux_hours_margin"}:
                if issue.code in emitted_margin:
                    continue
                emitted_margin.add(issue.code)
                if margin_count > 1:
                    metric = (
                        "equivalent-damage margin"
                        if issue.code == "equivalent_damage_margin"
                        else "lux-hour margin"
                    )
                    issue = issue.model_copy(update={
                        "message": (f"{margin_count} candidate blocks for exhibit "
                                    f"'{exhibit_id}' rejected by the {metric}; "
                                    f"first: {issue.message}")
                    })
            key = (issue.code, issue.severity, issue.message)
            if key not in seen_issue_keys:
                seen_issue_keys.add(key)
                issues.append(issue)

    # ---- Most-constrained-first backtracking -------------------------------
    max_blocks = max(1, request.max_blocks_per_exhibit)

    def candidate_sets_for(exhibit: Exhibit) -> list[list[dict]]:
        """All feasible ordered block lists (1..max_blocks) for an exhibit."""
        pool = per_exhibit_candidates[exhibit.id]
        spectral_exhibit = exhibit.sensitivity is not None
        # Cumulative budgets: the same margin rule that pruned individual
        # blocks must also hold for the combined block path.
        path_headroom = (
            exhibit.equivalent_damage_limit - exhibit.historical_equivalent_damage
            if spectral_exhibit and exhibit.equivalent_damage_limit is not None
            else exhibit.dose_limit_lux_hours - exhibit.historical_dose_lux_hours
        )
        paths: list[list[dict]] = []

        def path_charge(block: dict) -> Decimal:
            result = block["result"]
            if spectral_exhibit and result.equivalent_damage is not None:
                return result.equivalent_damage
            return result.lux_hours

        def extend(path: list[dict], remaining: list[dict]) -> None:
            if len(paths) >= request.max_candidate_paths:
                return
            if path:
                paths.append(list(path))
            if len(path) >= max_blocks:
                return
            cumulative = sum((path_charge(b) for b in path), Decimal(0))
            for index, candidate in enumerate(remaining):
                intervals = [(b["start"], b["end"]) for b in path]
                intervals.append((candidate["start"], candidate["end"]))
                ordered = sorted(intervals)
                # reject overlaps within the same exhibit
                if any(b_end > a_start for (_, b_end), (a_start, _) in zip(ordered, ordered[1:])):
                    continue
                if exhibit.minimum_rest_hours > 0 and len(ordered) >= 2:
                    gaps = [
                        Decimal((nxt[0] - prv[1]).total_seconds()) / Decimal(3600)
                        for prv, nxt in zip(ordered, ordered[1:])
                    ]
                    if gaps and min(gaps) < exhibit.minimum_rest_hours:
                        continue
                # cumulative margin guard for the combined path
                if cumulative + path_charge(candidate) > path_headroom + Decimal("0.0000001"):
                    continue
                extend(path + [candidate], remaining[index + 1:])

        extend([], pool)
        # Prefer single-block, lowest-damage paths first.
        paths.sort(key=lambda path: (
            len(path),
            sum(
                (b["result"].equivalent_damage
                 if b["result"].equivalent_damage is not None
                 else b["result"].lux_hours)
                for b in path
            ),
        ))
        return paths

    exhibit_paths = {
        exhibit.id: candidate_sets_for(exhibit)
        for exhibit in request.exhibits
    }

    # Capacity: count exhibits occupying gallery at any instant (blocks within
    # one exhibit path are disjoint by construction).
    def capacity_violation(gallery_id: str, path: list[dict], chosen: dict) -> bool:
        gallery = gallery_by_id[gallery_id]
        capacity = gallery.capacity
        events: list[tuple[datetime, int]] = []
        events.extend((b["start"], 1) for b in path if b["gallery_id"] == gallery_id)
        events.extend((b["end"], -1) for b in path if b["gallery_id"] == gallery_id)
        for other_path in chosen.values():
            events.extend((b["start"], 1) for b in other_path
                          if b["gallery_id"] == gallery_id)
            events.extend((b["end"], -1) for b in other_path
                          if b["gallery_id"] == gallery_id)
        events.sort()
        occupancy = 0
        for _, delta in events:
            occupancy += delta
            if occupancy > capacity:
                return True
        return False

    order = sorted(
        request.exhibits,
        key=lambda e: (len(exhibit_paths[e.id]), e.id),
    )
    chosen: dict[str, list[dict]] = {}

    def backtrack(position: int) -> bool:
        if position == len(order):
            return True
        exhibit = order[position]
        for path in exhibit_paths[exhibit.id]:
            touched_galleries = {b["gallery_id"] for b in path}
            if any(capacity_violation(gid, path, chosen) for gid in touched_galleries):
                continue
            chosen[exhibit.id] = path
            if backtrack(position + 1):
                return True
            del chosen[exhibit.id]
        return False

    complete = backtrack(0)

    # ---- Build response models ---------------------------------------------
    unplaced = []
    if not complete:
        for exhibit in order:
            if exhibit.id in chosen:
                continue
            pool = per_exhibit_candidates[exhibit.id]
            best_lux, best_hours, best_damage = getattr(exhibit, "_best", (None, None, None))
            margin_issue = next(
                (issue for issue in rejection_details.get(exhibit.id, [])
                 if issue.code in {"equivalent_damage_margin", "lux_hours_margin"}),
                None,
            )
            if not pool and not exhibit_paths[exhibit.id]:
                reason = "no feasible candidate block"
                code = "no_feasible_candidate"
                constraint = (
                    margin_issue.blocking_constraint
                    if margin_issue is not None
                    else ("spectral_action_function"
                          if spectral_rejections(exhibit, rejection_details) else None)
                )
            else:
                reason = "no combination satisfies gallery capacity and minimum rest"
                code = "capacity_or_rest_conflict"
                constraint = "gallery_capacity"
            unplaced.append({
                "exhibit_id": exhibit.id,
                "reason_code": code,
                "message": reason,
                "blocking_constraint": constraint,
                "candidate_count": len(pool),
                "best_candidate_dose_lux_hours": str(q(best_lux, QTY)) if best_lux is not None else None,
                "best_candidate_duration_hours": str(q(best_hours)) if best_hours is not None else None,
                "best_candidate_equivalent_damage": str(q(best_damage, QTY)) if best_damage is not None else None,
                "details": [
                    issue for issue in rejection_details.get(exhibit.id, [])
                    if issue.severity == Severity.ERROR
                ][:10],
            })

    # Flatten chosen blocks into placements for result computation.
    block_records: list[dict] = []
    for exhibit_id, path in chosen.items():
        for block in path:
            block_records.append({
                "exhibit_id": exhibit_id,
                "gallery_id": block["gallery_id"],
                "start": block["start"],
                "end": block["end"],
                "result": block["result"],
            })

    # Aggregate per exhibit and build schedule blocks with running ratios.
    exhibit_results = []
    schedule_blocks = []
    cumulative_damage: dict[str, Decimal] = {
        exhibit.id: exhibit.historical_equivalent_damage for exhibit in request.exhibits
    }
    cumulative_lux: dict[str, Decimal] = {
        exhibit.id: exhibit.historical_dose_lux_hours for exhibit in request.exhibits
    }

    blocks_sorted = sorted(block_records, key=lambda b: (b["start"], b["exhibit_id"]))
    results_by_exhibit: dict[str, list] = {}
    for record in blocks_sorted:
        results_by_exhibit.setdefault(record["exhibit_id"], []).append(record["result"])

    for record in blocks_sorted:
        exhibit = exhibit_by_id[record["exhibit_id"]]
        result = record["result"]
        cumulative_lux[exhibit.id] += result.lux_hours
        occupancy = cumulative_lux[exhibit.id] / exhibit.dose_limit_lux_hours
        block_kwargs = dict(
            exhibit_id=exhibit.id,
            gallery_id=record["gallery_id"],
            start=record["start"],
            end=record["end"],
            elapsed_hours=str(q(result.elapsed_hours)),
            planned_dose_lux_hours=str(q(result.lux_hours, QTY)),
            occupancy_ratio_after_block=str(q(occupancy, RATIO)),
        )
        if result.equivalent_damage is not None and exhibit.equivalent_damage_limit:
            cumulative_damage[exhibit.id] += result.equivalent_damage
            damage_ratio = cumulative_damage[exhibit.id] / exhibit.equivalent_damage_limit
            block_kwargs.update(
                equivalent_damage=str(q(result.equivalent_damage, QTY)),
                equivalent_damage_ratio_after_block=str(q(damage_ratio, RATIO)),
                bands=[
                    band_model for band_model in
                    (_placement_band_models(result))
                ],
            )
        schedule_blocks.append(ScheduleBlock(**block_kwargs))

    block_issues: list[Issue] = []
    for exhibit in request.exhibits:
        exhibit_results.append(build_exhibit_result(
            exhibit,
            results_by_exhibit.get(exhibit.id, []),
            block_issues,
            request.risk_threshold,
        ))

    # For exhibits chosen but also needing display minimum coverage across the
    # horizon, build_exhibit_result already validates. Attach extra issues.
    issues.extend(block_issues)

    total_lux = sum(
        (record["result"].lux_hours for record in blocks_sorted), Decimal(0)
    )
    total_damage = sum(
        (record["result"].equivalent_damage for record in blocks_sorted
         if record["result"].equivalent_damage is not None),
        Decimal(0),
    )
    spectral_blocks = [
        record for record in blocks_sorted
        if record["result"].equivalent_damage is not None
    ]
    gallery_assignments = len({
        (record["exhibit_id"], record["gallery_id"]) for record in blocks_sorted
    })
    exhibit_changes = len(blocks_sorted)

    max_occupancy = Decimal(0)
    max_damage_ratio = Decimal(0)
    for model in exhibit_results:
        max_occupancy = max(max_occupancy, Decimal(model.occupancy_ratio))
        if model.equivalent_damage_ratio is not None:
            max_damage_ratio = max(max_damage_ratio, Decimal(model.equivalent_damage_ratio))

    objective = {
        "exhibit_changes": exhibit_changes,
        "gallery_assignments": gallery_assignments,
        "maximum_occupancy_ratio": str(q(max_occupancy, RATIO)),
        "total_dose_lux_hours": str(q(total_lux, QTY)),
    }
    if spectral_blocks:
        objective["total_equivalent_damage"] = str(q(total_damage, QTY))
        objective["maximum_equivalent_damage_ratio"] = str(q(max_damage_ratio, RATIO))

    placements_flat = [
        Placement(
            exhibit_id=record["exhibit_id"],
            gallery_id=record["gallery_id"],
            start=record["start"],
            end=record["end"],
        )
        for record in blocks_sorted
    ]
    placement_models = []
    for index, (placement, record) in enumerate(zip(placements_flat, blocks_sorted)):
        from .engine import placement_result_to_model
        model = placement_result_to_model(record["result"], index)
        placement_models.append(model)

    has_error = any(issue.severity == Severity.ERROR for issue in issues)
    if not complete or has_error:
        status = Status.INFEASIBLE
    else:
        status = Status.FEASIBLE

    # The filter label reflects the rule actually applied: pure-classic
    # requests use the lux-hour margin; requests with any spectral exhibit
    # use the equivalent-damage margin. A per-exhibit map is also returned so
    # mixed requests stay unambiguous.
    per_exhibit_filter = {
        exhibit.id: (
            "equivalent_damage_margin"
            if exhibit.sensitivity is not None
            else "lux_hours_margin"
        )
        for exhibit in request.exhibits
    }
    active_filters = set(per_exhibit_filter.values())
    request_filter = (
        "lux_hours_margin"
        if active_filters == {"lux_hours_margin"}
        else "equivalent_damage_margin"
    )

    return {
        "kind": "rotation",
        "status": status,
        "horizon": TimeInterval(start=request.horizon_start, end=request.horizon_end),
        "change_dates": request.change_dates,
        "issues": issues,
        "objective": objective,
        "placements": placement_models,
        "schedule_blocks": schedule_blocks,
        "exhibits": exhibit_results,
        "unplaced": unplaced,
        "search": {
            "candidate_counts": {
                exhibit.id: len(per_exhibit_candidates[exhibit.id])
                for exhibit in request.exhibits
            },
            "path_counts": {
                exhibit.id: len(exhibit_paths[exhibit.id])
                for exhibit in request.exhibits
            },
            "max_candidate_paths": request.max_candidate_paths,
            "filter": request_filter,
            "filters": per_exhibit_filter,
            "margin_rejections": margin_rejections,
        },
    }


def spectral_rejections(exhibit: Exhibit, details: dict) -> bool:
    return any(
        issue.code
        in {"equivalent_damage_margin", "lux_hours_margin", "spectrum_no_overlap"}
        for issue in details.get(exhibit.id, [])
    )


def _placement_band_models(result):
    from .models import BandContribution
    models = []
    for aggregate in result.bands.values():
        fraction = (
            Decimal(str(aggregate.damage)) / result.equivalent_damage
            if result.equivalent_damage and result.equivalent_damage != 0
            else Decimal(0)
        )
        models.append(BandContribution(
            wavelength_start_nm=f"{aggregate.start_nm:g}",
            wavelength_end_nm=f"{aggregate.end_nm:g}",
            band_name=aggregate.name,
            equivalent_damage=str(q(Decimal(str(aggregate.damage)), QTY)),
            equivalent_damage_fraction=str(q(fraction, RATIO)),
        ))
    return models
