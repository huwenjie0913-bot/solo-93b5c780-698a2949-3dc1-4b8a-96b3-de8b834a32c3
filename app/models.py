"""Pydantic models for the museum light-dose scheduling API."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", ser_json_inf_nan="error")

    @field_validator("*", mode="before")
    @classmethod
    def _reject_none_in_optional_lists(cls, value: Any) -> Any:
        # Pydantic handles optionality; this hook is kept centralized for future
        # input normalization while still rejecting unknown fields.
        return value


class Severity(str, Enum):
    ERROR = "error"
    RISK = "risk"


class Status(str, Enum):
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    INCOMPLETE = "incomplete"


class MaterialType(str, Enum):
    TEXTILE = "textile"
    PHOTOGRAPH = "photograph"
    DYED_SPECIMEN = "dyed_specimen"
    OTHER = "other"


class WeeklyOpen(StrictModel):
    weekday: int = Field(ge=0, le=6, description="Monday=0 through Sunday=6.")
    open_time: str = Field(description="Gallery local opening time, HH:MM:SS or HH:MM.")
    close_time: str = Field(description="Gallery local closing time, HH:MM:SS or HH:MM.")


class ClosedException(StrictModel):
    name: str | None = Field(default=None, description="Optional closure name.")
    start: datetime
    end: datetime
    expected_open: bool = Field(
        default=True,
        description="True for an unexpected cancellation of scheduled opening; "
        "false for a normal no-open date.",
    )


class IlluminationSegment(StrictModel):
    start: datetime
    end: datetime
    lux: Decimal = Field(ge=0)

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, value: datetime, info):
        start = info.data.get("start")
        if start is not None and value <= start:
            raise ValueError("end must be after start")
        return value


class GalleryCapacityRule(StrictModel):
    """Number of exhibits that may occupy the gallery simultaneously."""

    default: int = Field(default=1, ge=1)


class Gallery(StrictModel):
    id: str = Field(min_length=1)
    name: str | None = None
    timezone: str = Field(default="UTC")
    weekly_open: list[WeeklyOpen] = Field(default_factory=list)
    closed_exceptions: list[ClosedException] = Field(default_factory=list)
    illumination: list[IlluminationSegment] = Field(default_factory=list)
    capacity: int = Field(default=1, ge=1)


class Exhibit(StrictModel):
    id: str = Field(min_length=1)
    name: str | None = None
    material: MaterialType = MaterialType.OTHER
    historical_dose_lux_hours: Decimal = Field(default=Decimal(0), ge=0)
    dose_limit_lux_hours: Decimal = Field(gt=0)
    minimum_display_hours: Decimal = Field(default=Decimal(0), ge=0)
    minimum_rest_hours: Decimal = Field(default=Decimal(0), ge=0)
    candidate_gallery_ids: list[str] = Field(default_factory=list)


class Placement(StrictModel):
    id: str | None = Field(default=None, description="Optional source placement identifier.")
    exhibit_id: str
    gallery_id: str
    start: datetime
    end: datetime

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, value: datetime, info):
        start = info.data.get("start")
        if start is not None and value <= start:
            raise ValueError("end must be after start")
        return value


class DoseComputeRequest(StrictModel):
    kind: Literal["dose"] = "dose"
    horizon_start: datetime
    horizon_end: datetime
    galleries: list[Gallery]
    exhibits: list[Exhibit]
    placements: list[Placement]
    risk_threshold: Decimal = Field(
        default=Decimal("0.90"), ge=0, le=1, description="Occupancy ratio risk threshold."
    )

    @field_validator("horizon_end")
    @classmethod
    def _end_after_start(cls, value: datetime, info):
        start = info.data.get("horizon_start")
        if start is not None and value <= start:
            raise ValueError("horizon_end must be after horizon_start")
        return value


class RotationRequest(StrictModel):
    kind: Literal["rotation"] = "rotation"
    horizon_start: datetime
    horizon_end: datetime
    galleries: list[Gallery]
    exhibits: list[Exhibit]
    change_dates: list[datetime] = Field(
        default_factory=list,
        description="Allowed installation/deinstallation instants. Horizon bounds are added automatically.",
    )
    risk_threshold: Decimal = Field(default=Decimal("0.90"), ge=0, le=1)
    max_blocks_per_exhibit: int = Field(default=2, ge=1, le=6)
    max_candidate_paths: int = Field(default=2000, ge=1, le=100000)

    @field_validator("horizon_end")
    @classmethod
    def _end_after_start(cls, value: datetime, info):
        start = info.data.get("horizon_start")
        if start is not None and value <= start:
            raise ValueError("horizon_end must be after horizon_start")
        return value


class Issue(StrictModel):
    code: str
    severity: Severity
    message: str
    location: dict[str, Any] = Field(default_factory=dict)
    blocking_constraint: str | None = None
    value: str | None = None
    limit: str | None = None


class TimeInterval(StrictModel):
    start: datetime
    end: datetime


class SegmentContribution(StrictModel):
    segment_start: datetime
    segment_end: datetime
    overlap_start: datetime
    overlap_end: datetime
    lux: str
    elapsed_hours: str
    lux_hours: str


class DailyContribution(StrictModel):
    gallery_date: str
    local_day_start: datetime
    local_day_end: datetime
    open_hours: str
    illuminated_hours: str
    lux_hours: str
    segments: list[SegmentContribution] = Field(default_factory=list)


class PlacementDoseResult(StrictModel):
    placement_index: int
    placement_id: str | None = None
    exhibit_id: str
    gallery_id: str
    start: datetime
    end: datetime
    elapsed_hours: str
    open_hours: str
    illuminated_hours: str
    lux_hours: str
    daily: list[DailyContribution] = Field(default_factory=list)


class ExhibitDoseResult(StrictModel):
    exhibit_id: str
    historical_dose_lux_hours: str
    planned_dose_lux_hours: str
    total_dose_lux_hours: str
    dose_limit_lux_hours: str
    remaining_lux_hours: str
    occupancy_ratio: str
    remaining_ratio: str
    over_limit_by_lux_hours: str
    placements: list[PlacementDoseResult] = Field(default_factory=list)
    schedule_blocks: list["ScheduleBlock"] = Field(default_factory=list)
    minimum_display_elapsed_hours: str
    actual_display_elapsed_hours: str
    minimum_rest_hours: str
    minimum_rest_gap_hours: str | None = None


class DoseComputeResponse(StrictModel):
    kind: Literal["dose"] = "dose"
    status: Status
    horizon: TimeInterval
    issues: list[Issue] = Field(default_factory=list)
    exhibits: list[ExhibitDoseResult] = Field(default_factory=list)
    calendar_intervals: dict[str, list[TimeInterval]] = Field(default_factory=dict)


class ScheduleBlock(StrictModel):
    exhibit_id: str
    gallery_id: str
    start: datetime
    end: datetime
    elapsed_hours: str
    planned_dose_lux_hours: str
    occupancy_ratio_after_block: str


class RotationObjective(StrictModel):
    exhibit_changes: int
    gallery_assignments: int
    maximum_occupancy_ratio: str
    total_dose_lux_hours: str


class UnplacedExhibit(StrictModel):
    exhibit_id: str
    reason_code: str
    message: str
    blocking_constraint: str | None = None
    candidate_count: int
    best_candidate_dose_lux_hours: str | None = None
    best_candidate_duration_hours: str | None = None
    details: list[Issue] = Field(default_factory=list)


class RotationResponse(StrictModel):
    kind: Literal["rotation"] = "rotation"
    status: Status
    horizon: TimeInterval
    change_dates: list[datetime] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    objective: RotationObjective | None = None
    placements: list[PlacementDoseResult] = Field(default_factory=list)
    schedule_blocks: list[ScheduleBlock] = Field(default_factory=list)
    exhibits: list[ExhibitDoseResult] = Field(default_factory=list)
    unplaced: list[UnplacedExhibit] = Field(default_factory=list)
    search: dict[str, Any] = Field(default_factory=dict)


class SnapshotSummary(StrictModel):
    snapshot_id: int
    created_at: datetime
    name: str
    request_kind: str
    status: str
    objective: RotationObjective | None = None


class SnapshotSaveRequest(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    request: DoseComputeRequest | RotationRequest = Field(discriminator="kind")


class SnapshotSaveResponse(StrictModel):
    snapshot_id: int
    saved_at: datetime
    name: str
    request_kind: str
    result_status: str


class SnapshotResponse(StrictModel):
    snapshot: SnapshotSummary
    request: dict[str, Any]
    result: dict[str, Any]


class SnapshotListResponse(StrictModel):
    snapshots: list[SnapshotSummary]


class BlockDiff(StrictModel):
    exhibit_id: str
    before: ScheduleBlock | None = None
    after: ScheduleBlock | None = None
    change_type: Literal["added", "removed", "moved", "modified", "unchanged"]


class ExhibitDoseDiff(StrictModel):
    exhibit_id: str
    before_total_lux_hours: str
    after_total_lux_hours: str
    delta_lux_hours: str
    before_occupancy_ratio: str
    after_occupancy_ratio: str
    delta_occupancy_ratio: str
    before_remaining_lux_hours: str
    after_remaining_lux_hours: str
    delta_remaining_lux_hours: str


class SnapshotDiffResponse(StrictModel):
    base_snapshot_id: int
    target_snapshot_id: int | None = None
    recomputed: bool
    status_changed: bool
    base_status: str
    target_status: str
    dose_differences: list[ExhibitDoseDiff] = Field(default_factory=list)
    schedule_differences: list[BlockDiff] = Field(default_factory=list)
    issue_count_delta: int
    objective_delta: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(StrictModel):
    status: Literal["ok"]


ExhibitDoseResult.model_rebuild()
DoseComputeResponse.model_rebuild()
