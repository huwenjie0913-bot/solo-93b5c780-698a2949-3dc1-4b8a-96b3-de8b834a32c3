"""Rotation search: spectral margin filtering, capacity, classic fallback."""
from __future__ import annotations


def _rotation(galleries, exhibits, change_dates, **extra):
    request = {
        "kind": "rotation",
        "horizon_start": "2026-01-01T00:00:00Z",
        "horizon_end": "2026-01-04T00:00:00Z",
        "change_dates": change_dates,
        "galleries": galleries,
        "exhibits": exhibits,
    }
    request.update(extra)
    return request


def _gallery(gallery_id="G1", *, spectra=True, capacity=1):
    illumination = []
    for day in range(1, 4):
        segment = {
            "start": f"2026-01-{day:02d}T09:00:00Z",
            "end": f"2026-01-{day:02d}T17:00:00Z",
            "lux": "100",
        }
        if spectra:
            segment["spectrum"] = {"wavelengths_nm": ["400", "700"],
                                   "values": ["1", "1"]}
        illumination.append(segment)
    return {
        "id": gallery_id,
        "capacity": capacity,
        "weekly_open": [
            {"weekday": day, "open_time": "09:00", "close_time": "17:00"}
            for day in range(7)
        ],
        "illumination": illumination,
    }


_CHANGE_DATES = [
    "2026-01-01T09:00:00Z",
    "2026-01-02T09:00:00Z",
    "2026-01-03T09:00:00Z",
]


def _spectral_exhibit(exhibit_id="P1", damage_limit="5000",
                      sensitivity=("1", "1"), candidate_galleries=None):
    exhibit = {
        "id": exhibit_id,
        "material": "photograph",
        "dose_limit_lux_hours": "1000000",
        "sensitivity": {"wavelengths_nm": ["400", "700"],
                        "values": list(sensitivity)},
        "equivalent_damage_limit": damage_limit,
        "minimum_display_hours": "4",
    }
    if candidate_galleries is not None:
        exhibit["candidate_gallery_ids"] = candidate_galleries
    return exhibit


def test_margin_filter_selects_fewer_days_when_limit_tight(client):
    # each day is 800 lux-h with flat sensitivity -> factor 1 -> 800 damage
    request = _rotation(
        [_gallery()], [_spectral_exhibit(damage_limit="900")], _CHANGE_DATES
    )
    data = client.post("/rotation", json=request).json()
    assert data["status"] == "feasible"
    assert len(data["schedule_blocks"]) == 1
    block = data["schedule_blocks"][0]
    assert block["equivalent_damage"] == "800.000000"
    assert block["equivalent_damage_ratio_after_block"] == "0.888889"
    assert data["search"]["filter"] == "equivalent_damage_margin"


def test_margin_filter_makes_tight_limit_infeasible(client):
    request = _rotation(
        [_gallery()], [_spectral_exhibit(damage_limit="700")], _CHANGE_DATES
    )
    data = client.post("/rotation", json=request).json()
    assert data["status"] == "infeasible"
    unplaced = data["unplaced"][0]
    assert unplaced["exhibit_id"] == "P1"
    assert unplaced["blocking_constraint"] == "equivalent_damage_limit"
    assert unplaced["candidate_count"] == 0
    assert unplaced["best_candidate_equivalent_damage"] == "800.000000"
    assert unplaced["best_candidate_duration_hours"] == "8.0000"
    assert any(i["code"] == "equivalent_damage_margin" for i in data["issues"])
    assert data["search"]["margin_rejections"].get("P1", 0) >= 1


def test_led_needs_fewer_blocks_than_daylight_under_same_limit(client):
    uv_leaning = ("1", "0.2")
    led_gallery = {
        "id": "LED",
        "capacity": 1,
        "weekly_open": [
            {"weekday": day, "open_time": "09:00", "close_time": "17:00"}
            for day in range(7)
        ],
        "illumination": [
            {
                "start": f"2026-01-{day:02d}T09:00:00Z",
                "end": f"2026-01-{day:02d}T17:00:00Z",
                "lux": "100",
                "spectrum": {"wavelengths_nm": ["400", "440", "460", "700"],
                             "values": ["0.05", "1", "1", "0.05"]},
            }
            for day in range(1, 4)
        ],
    }
    daylight_gallery = _gallery("DAY")
    exhibits = [
        _spectral_exhibit("LED_EX", damage_limit="700",
                          sensitivity=uv_leaning, candidate_galleries=["LED"]),
        _spectral_exhibit("DAY_EX", damage_limit="700",
                          sensitivity=uv_leaning, candidate_galleries=["DAY"]),
    ]
    data = client.post(
        "/rotation",
        json=_rotation([led_gallery, daylight_gallery], exhibits, _CHANGE_DATES),
    ).json()
    assert data["status"] == "feasible"
    damage_by_exhibit = {}
    blocks_by_exhibit = {}
    for model in data["exhibits"]:
        damage_by_exhibit[model["exhibit_id"]] = float(
            model["planned_equivalent_damage"])
    for block in data["schedule_blocks"]:
        blocks_by_exhibit.setdefault(block["exhibit_id"], 0)
        blocks_by_exhibit[block["exhibit_id"]] += 1
    # equal lux-hours per day, LED damages the UV-leaning material faster
    assert damage_by_exhibit["LED_EX"] > damage_by_exhibit["DAY_EX"]
    # both exhibits fit one day under the 700 ceiling; two daylight days (960)
    # and two LED days (1126) are rejected by the margin filter
    assert blocks_by_exhibit == {"LED_EX": 1, "DAY_EX": 1}
    assert abs(damage_by_exhibit["LED_EX"] - 562.927478) < 1e-3
    assert abs(damage_by_exhibit["DAY_EX"] - 480.0) < 1e-6


def test_capacity_limits_simultaneous_blocks(client):
    galleries = [_gallery("G1", capacity=1)]
    exhibits = [
        _spectral_exhibit("P1", damage_limit="100000"),
        _spectral_exhibit("P2", damage_limit="100000"),
    ]
    data = client.post(
        "/rotation",
        json=_rotation(galleries, exhibits, _CHANGE_DATES),
    ).json()
    # only one can occupy the single gallery at a time; with three disjoint
    # days both exhibits must be placeable on different days
    assert data["status"] == "feasible"
    assert len(data["unplaced"]) == 0
    placed = {block["exhibit_id"] for block in data["schedule_blocks"]}
    assert placed == {"P1", "P2"}

    galleries[0]["capacity"] = 2
    both = client.post(
        "/rotation",
        json=_rotation(galleries, exhibits, _CHANGE_DATES),
    ).json()
    assert both["status"] == "feasible"


def test_classic_rotation_uses_lux_hours(client):
    classic_exhibit = [{
        "id": "T1", "dose_limit_lux_hours": "2000",
        "minimum_display_hours": "4",
    }]
    request = _rotation([_gallery(spectra=False)], classic_exhibit, _CHANGE_DATES)
    data = client.post("/rotation", json=request).json()
    assert data["status"] == "feasible"
    assert data["objective"]["total_dose_lux_hours"] == "800.000000"
    assert data["objective"].get("total_equivalent_damage") is None
    for block in data["schedule_blocks"]:
        assert block["equivalent_damage"] is None
        assert block["equivalent_damage_ratio_after_block"] is None
    assert data["exhibits"][0]["spectral_mode"] is False
    assert data["exhibits"][0]["equivalent_damage_ratio"] is None


def test_classic_rotation_prunes_blocks_over_lux_hour_margin(client):
    # every candidate block carries 800 lux-hours; a 700 lux-hour ceiling
    # (no historical dose) must prune every block, not silently keep one
    classic_exhibit = [{
        "id": "T1", "dose_limit_lux_hours": "700",
        "minimum_display_hours": "4",
    }]
    request = _rotation([_gallery(spectra=False)], classic_exhibit, _CHANGE_DATES)
    data = client.post("/rotation", json=request).json()
    assert data["status"] == "infeasible"
    assert data["search"]["filter"] == "lux_hours_margin"
    assert data["search"]["filters"]["T1"] == "lux_hours_margin"
    assert data["search"]["candidate_counts"]["T1"] == 0
    assert data["search"]["margin_rejections"]["T1"] >= 1
    margin_issue = next(i for i in data["issues"] if i["code"] == "lux_hours_margin")
    assert margin_issue["severity"] == "risk"
    assert margin_issue["blocking_constraint"] == "dose_limit_lux_hours"
    assert margin_issue["value"] == "800.000000"
    assert margin_issue["limit"] == "700.000000"

    unplaced = data["unplaced"][0]
    assert unplaced["exhibit_id"] == "T1"
    assert unplaced["reason_code"] == "no_feasible_candidate"
    assert unplaced["blocking_constraint"] == "dose_limit_lux_hours"
    assert unplaced["best_candidate_dose_lux_hours"] == "800.000000"
    assert unplaced["best_candidate_equivalent_damage"] is None


def test_classic_rotation_cumulative_lux_hour_margin_guards_paths(client):
    # single 800-lux-h block fits a 1000 ceiling, but two blocks (1600) must
    # be pruned by the cumulative path-level margin guard
    classic_exhibit = [{
        "id": "T1", "dose_limit_lux_hours": "1000",
        "minimum_display_hours": "4",
    }]
    request = _rotation([_gallery(spectra=False)], classic_exhibit, _CHANGE_DATES)
    data = client.post("/rotation", json=request).json()
    assert data["status"] == "feasible"
    assert len(data["schedule_blocks"]) == 1
    assert data["schedule_blocks"][0]["planned_dose_lux_hours"] == "800.000000"


def test_historical_dose_shrinks_classic_lux_headroom(client):
    # 500 historical + a fresh 800 block = 1300 > limit 1200 -> pruned
    classic_exhibit = [{
        "id": "T1", "dose_limit_lux_hours": "1200",
        "historical_dose_lux_hours": "500",
        "minimum_display_hours": "4",
    }]
    request = _rotation([_gallery(spectra=False)], classic_exhibit, _CHANGE_DATES)
    data = client.post("/rotation", json=request).json()
    assert data["status"] == "infeasible"
    margin_issue = next(i for i in data["issues"] if i["code"] == "lux_hours_margin")
    # remaining headroom is 1200 - 500 = 700
    assert margin_issue["limit"] == "700.000000"


def test_mixed_request_reports_per_exhibit_filters(client):
    # one spectral exhibit and one classic exhibit share the horizon; the
    # top-level filter is the spectral rule, the classic exhibit is labeled
    # separately under search.filters
    exhibits = [
        _spectral_exhibit("P1", damage_limit="100000"),
        {"id": "T1", "dose_limit_lux_hours": "100000",
         "minimum_display_hours": "4"},
    ]
    request = _rotation([_gallery()], exhibits, _CHANGE_DATES)
    data = client.post("/rotation", json=request).json()
    assert data["status"] == "feasible"
    assert data["search"]["filter"] == "equivalent_damage_margin"
    assert data["search"]["filters"] == {
        "P1": "equivalent_damage_margin",
        "T1": "lux_hours_margin",
    }


def test_zero_illumination_intervals_not_offered_as_blocks(client):
    # change date in the middle of the night must not produce a 0-dose block
    change_dates = _CHANGE_DATES + ["2026-01-02T03:00:00Z"]
    request = _rotation(
        [_gallery()], [_spectral_exhibit(damage_limit="900")], change_dates
    )
    data = client.post("/rotation", json=request).json()
    assert data["status"] == "feasible"
    for block in data["schedule_blocks"]:
        assert float(block["planned_dose_lux_hours"]) > 0


def test_response_still_carries_legacy_fields(client):
    request = _rotation(
        [_gallery()], [_spectral_exhibit(damage_limit="100000")], _CHANGE_DATES
    )
    data = client.post("/rotation", json=request).json()
    exhibit = data["exhibits"][0]
    # legacy lux-hour fields are populated alongside spectral fields
    assert float(exhibit["planned_dose_lux_hours"]) > 0
    assert exhibit["dose_limit_lux_hours"] == "1000000.000000"
    assert exhibit["planned_equivalent_damage"] is not None
