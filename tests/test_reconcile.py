"""End-to-end behaviour of /exposure-reconcile via the ASGI app."""
from __future__ import annotations

DAY_SPECTRUM = {"wavelengths_nm": ["400", "700"], "values": ["1", "1"]}


def _reconcile_request(galleries, exhibits, placements, readings, **extra):
    request = {
        "kind": "exposure_reconcile",
        "horizon_start": "2026-01-01T00:00:00Z",
        "horizon_end": "2026-01-03T00:00:00Z",
        "galleries": galleries,
        "exhibits": exhibits,
        "placements": placements,
        "readings": readings,
    }
    request.update(extra)
    return request


def _gallery(gallery_id="G1", illumination=None, timezone="UTC"):
    return {
        "id": gallery_id,
        "timezone": timezone,
        "weekly_open": [
            {"weekday": day, "open_time": "09:00", "close_time": "17:00"}
            for day in range(7)
        ],
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


def _exhibit(exhibit_id="E1", limit="10000", historical="0", **extra):
    exhibit = {
        "id": exhibit_id,
        "dose_limit_lux_hours": limit,
        "historical_dose_lux_hours": historical,
    }
    exhibit.update(extra)
    return exhibit


def _placement(exhibit_id="E1", gallery_id="G1",
               start="2026-01-01T09:00:00Z", end="2026-01-01T17:00:00Z"):
    return {"exhibit_id": exhibit_id, "gallery_id": gallery_id,
            "start": start, "end": end}


def _readings(gallery_id, hours, lux="100", day=1):
    return [
        {"gallery_id": gallery_id,
         "timestamp": f"2026-01-{day:02d}T{hour:02d}:00:00Z",
         "lux": str(lux)}
        for hour in hours
    ]


def _hourly(gallery_id, start_hour=9, end_hour=17, lux="100", day=1):
    return _readings(gallery_id, range(start_hour, end_hour + 1), lux, day)


# ------------------------------------------------------------------- normal path


def test_reconcile_matches_plan_when_readings_follow_schedule(client):
    request = _reconcile_request(
        [_gallery()],
        [_exhibit()],
        [_placement()],
        _hourly("G1"),
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["kind"] == "exposure_reconcile"
    assert data["status"] == "feasible"
    assert data["issues"] == []
    exhibit = data["exhibits"][0]
    assert exhibit["measured_lux_hours"] == "800.000000"
    assert exhibit["planned_dose_lux_hours"] == "800.000000"
    assert exhibit["delta_lux_hours"] == "0.000000"
    assert exhibit["coverage_ratio"] == "1.000000"
    assert exhibit["total_measured_lux_hours"] == "800.000000"
    assert exhibit["corrected_remaining_lux_hours"] == "9200.000000"
    assert exhibit["measured_occupancy_ratio"] == "0.080000"
    placement = exhibit["placements"][0]
    assert placement["reading_count"] == 9
    assert placement["covered_hours"] == "8.0000"
    assert placement["coverage_ratio"] == "1.000000"
    assert placement["daily"][0]["gallery_date"] == "2026-01-01"
    assert placement["daily"][0]["lux_hours"] == "800.000000"
    assert placement["daily"][0]["readings"] == 9


def test_dimmed_gallery_measures_less_and_corrects_remaining(client):
    request = _reconcile_request(
        [_gallery()],
        [_exhibit(limit="10000", historical="100")],
        [_placement()],
        _hourly("G1", lux="50"),
    )
    data = client.post("/exposure-reconcile", json=request).json()
    exhibit = data["exhibits"][0]
    assert exhibit["measured_lux_hours"] == "400.000000"
    assert exhibit["planned_dose_lux_hours"] == "800.000000"
    assert exhibit["delta_lux_hours"] == "-400.000000"
    # corrected remaining uses measured, not planned: 10000 - (100 + 400)
    assert exhibit["corrected_remaining_lux_hours"] == "9500.000000"
    assert exhibit["total_measured_lux_hours"] == "500.000000"


def test_trapezoidal_integration_with_varying_lux(client):
    readings = _readings("G1", [9], "0") + _readings("G1", [13], "100") \
        + _readings("G1", [17], "0")
    readings.sort(key=lambda r: r["timestamp"])
    request = _reconcile_request(
        [_gallery()], [_exhibit()], [_placement()], readings,
        max_sampling_gap_hours="4",
    )
    data = client.post("/exposure-reconcile", json=request).json()
    exhibit = data["exhibits"][0]
    # (0+100)/2 * 4h + (100+0)/2 * 4h
    assert exhibit["measured_lux_hours"] == "400.000000"
    assert exhibit["coverage_ratio"] == "1.000000"
    assert data["status"] == "feasible"


def test_readings_with_timezone_offsets_are_normalized(client):
    readings = [
        {"gallery_id": "G1",
         "timestamp": f"2026-01-01T{hour:02d}:00:00+01:00", "lux": "100"}
        for hour in range(10, 19)  # 09:00Z .. 17:00Z
    ]
    request = _reconcile_request(
        [_gallery()], [_exhibit()], [_placement()], readings,
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["status"] == "feasible"
    assert data["exhibits"][0]["measured_lux_hours"] == "800.000000"


def test_two_exhibits_in_one_gallery_share_the_same_readings(client):
    request = _reconcile_request(
        [_gallery()],
        [_exhibit("E1", limit="10000"), _exhibit("E2", limit="2000")],
        [_placement("E1"), _placement("E2")],
        _hourly("G1"),
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["status"] == "feasible"
    assert [e["measured_lux_hours"] for e in data["exhibits"]] == [
        "800.000000", "800.000000",
    ]


# --------------------------------------------------------------------- gap cuts


def test_sampling_gap_cuts_integration_without_interpolation(client):
    readings = _readings("G1", [9, 10, 15, 16, 17], "100")
    request = _reconcile_request(
        [_gallery()], [_exhibit()], [_placement()], readings,
        max_sampling_gap_hours="2",
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["status"] == "incomplete"
    exhibit = data["exhibits"][0]
    # 09-10 (100) + 15-17 (200); the 10:00->15:00 hole is not interpolated
    assert exhibit["measured_lux_hours"] == "300.000000"
    assert exhibit["coverage_ratio"] == "0.375000"
    gaps = [i for i in data["issues"] if i["code"] == "sampling_gap"]
    assert len(gaps) == 1
    assert gaps[0]["severity"] == "risk"
    assert gaps[0]["value"] == "5.0000"
    assert gaps[0]["limit"] == "2"
    assert gaps[0]["location"]["gap_start"] == "2026-01-01T10:00:00"
    assert gaps[0]["location"]["gap_end"] == "2026-01-01T15:00:00"


def test_max_sampling_gap_is_configurable(client):
    readings = _readings("G1", [9, 10, 15, 16, 17], "100")
    request = _reconcile_request(
        [_gallery()], [_exhibit()], [_placement()], readings,
        max_sampling_gap_hours="6",
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["status"] == "feasible"
    assert data["exhibits"][0]["measured_lux_hours"] == "800.000000"
    assert not any(i["code"] == "sampling_gap" for i in data["issues"])


def test_placement_without_readings_is_one_full_gap(client):
    request = _reconcile_request(
        [_gallery()], [_exhibit()], [_placement()], [],
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["status"] == "incomplete"
    exhibit = data["exhibits"][0]
    assert exhibit["measured_lux_hours"] == "0.000000"
    assert exhibit["coverage_ratio"] == "0.000000"
    assert exhibit["corrected_remaining_lux_hours"] == "10000.000000"
    gaps = [i for i in data["issues"] if i["code"] == "sampling_gap"]
    assert len(gaps) == 1
    assert "no readings fall within the placement" in gaps[0]["message"]


def test_leading_and_trailing_gaps_are_reported(client):
    readings = _readings("G1", [11, 12, 13], "100")
    request = _reconcile_request(
        [_gallery()], [_exhibit()], [_placement()], readings,
    )
    data = client.post("/exposure-reconcile", json=request).json()
    exhibit = data["exhibits"][0]
    assert exhibit["measured_lux_hours"] == "200.000000"
    assert exhibit["coverage_ratio"] == "0.250000"
    gaps = [i for i in data["issues"] if i["code"] == "sampling_gap"]
    assert len(gaps) == 2  # 09:00->11:00 and 13:00->17:00


# ------------------------------------------------------------------ daily detail


def test_daily_breakdown_spans_local_days_and_cuts_overnight(client):
    illumination = [
        {"start": "2026-01-01T09:00:00Z", "end": "2026-01-01T17:00:00Z",
         "lux": "100"},
        {"start": "2026-01-02T09:00:00Z", "end": "2026-01-02T17:00:00Z",
         "lux": "100"},
    ]
    readings = _hourly("G1", day=1) + _hourly("G1", day=2)
    request = _reconcile_request(
        [_gallery(illumination=illumination)],
        [_exhibit()],
        [_placement(start="2026-01-01T09:00:00Z", end="2026-01-02T17:00:00Z")],
        readings,
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["status"] == "incomplete"  # overnight hole between 17:00 and 09:00
    exhibit = data["exhibits"][0]
    assert exhibit["measured_lux_hours"] == "1600.000000"
    assert exhibit["planned_dose_lux_hours"] == "1600.000000"
    placement = exhibit["placements"][0]
    assert [d["gallery_date"] for d in placement["daily"]] == [
        "2026-01-01", "2026-01-02",
    ]
    assert [d["lux_hours"] for d in placement["daily"]] == [
        "800.000000", "800.000000",
    ]
    assert [d["readings"] for d in placement["daily"]] == [9, 9]
    gaps = [i for i in data["issues"] if i["code"] == "sampling_gap"]
    assert len(gaps) == 1
    assert gaps[0]["location"]["gap_start"] == "2026-01-01T17:00:00"
    assert gaps[0]["location"]["gap_end"] == "2026-01-02T09:00:00"


# --------------------------------------------------------------- spectral mode


def _spectral_exhibit(damage_limit="600", historical_damage="0"):
    return _exhibit(
        "P1",
        limit="100000",
        material="photograph",
        sensitivity={"wavelengths_nm": ["400", "700"], "values": ["1", "0.2"]},
        equivalent_damage_limit=damage_limit,
        historical_equivalent_damage=historical_damage,
    )


def _spectral_gallery():
    return _gallery(illumination=[{
        "start": "2026-01-01T09:00:00Z",
        "end": "2026-01-01T17:00:00Z",
        "lux": "100",
        "spectrum": DAY_SPECTRUM,
    }])


def test_spectral_measured_damage_is_recomputed_from_readings(client):
    request = _reconcile_request(
        [_spectral_gallery()],
        [_spectral_exhibit()],
        [_placement("P1")],
        _hourly("G1", lux="50"),
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["status"] == "feasible"
    exhibit = data["exhibits"][0]
    assert exhibit["spectral_mode"] is True
    # flat light, sensitivity 1 -> 0.2: factor 0.6
    assert exhibit["measured_lux_hours"] == "400.000000"
    assert exhibit["measured_equivalent_damage"] == "240.000000"
    assert exhibit["planned_equivalent_damage"] == "480.000000"
    assert exhibit["delta_equivalent_damage"] == "-240.000000"
    assert exhibit["corrected_remaining_equivalent_damage"] == "360.000000"
    assert exhibit["measured_equivalent_damage_ratio"] == "0.400000"
    placement = exhibit["placements"][0]
    assert placement["measured_equivalent_damage"] == "240.000000"
    assert placement["daily"][0]["equivalent_damage"] == "240.000000"


def test_measured_light_outside_illumination_falls_back_with_issue(client):
    request = _reconcile_request(
        [_spectral_gallery()],
        [_spectral_exhibit(damage_limit="1000")],
        [_placement("P1", start="2026-01-01T09:00:00Z",
                    end="2026-01-01T19:00:00Z")],
        _hourly("G1", start_hour=9, end_hour=19),
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["status"] == "feasible"
    exhibit = data["exhibits"][0]
    # 09-17 charged at factor 0.6 (480), 17-19 outside the segment at 1:1 (200)
    assert exhibit["measured_lux_hours"] == "1000.000000"
    assert exhibit["measured_equivalent_damage"] == "680.000000"
    assert exhibit["planned_equivalent_damage"] == "480.000000"
    assert any(i["code"] == "measured_outside_illumination"
               and i["severity"] == "risk" for i in data["issues"])


def test_spectral_measured_over_damage_limit_is_error(client):
    request = _reconcile_request(
        [_spectral_gallery()],
        [_spectral_exhibit(damage_limit="400")],
        [_placement("P1")],
        _hourly("G1"),  # 800 lux-h * 0.6 = 480 damage > 400
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["status"] == "infeasible"
    exhibit = data["exhibits"][0]
    assert exhibit["over_limit_by_equivalent_damage"] == "80.000000"
    assert exhibit["corrected_remaining_equivalent_damage"] == "-80.000000"
    assert any(i["code"] == "equivalent_damage_limit_exceeded"
               and i["severity"] == "error" for i in data["issues"])


# ------------------------------------------------------- limit and risk issues


def test_measured_over_limit_is_blocking_error(client):
    request = _reconcile_request(
        [_gallery()], [_exhibit(limit="500")], [_placement()], _hourly("G1"),
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["status"] == "infeasible"
    exhibit = data["exhibits"][0]
    assert exhibit["over_limit_by_lux_hours"] == "300.000000"
    assert exhibit["corrected_remaining_lux_hours"] == "-300.000000"
    issue = next(i for i in data["issues"] if i["code"] == "dose_limit_exceeded")
    assert issue["severity"] == "error"
    assert issue["blocking_constraint"] == "dose_limit_lux_hours"
    assert "measured" in issue["message"]


def test_measured_near_threshold_raises_risk(client):
    request = _reconcile_request(
        [_gallery()], [_exhibit(limit="850")], [_placement()], _hourly("G1"),
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["status"] == "feasible"
    exhibit = data["exhibits"][0]
    assert exhibit["measured_occupancy_ratio"] == "0.941176"
    issue = next(i for i in data["issues"] if i["code"] == "dose_occupancy_risk")
    assert issue["severity"] == "risk"


# ------------------------------------------------------------- unassigned data


def test_reading_outside_placement_is_unassigned(client):
    readings = _hourly("G1") + _readings("G1", [20], "30")
    request = _reconcile_request(
        [_gallery()], [_exhibit()], [_placement()], readings,
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["status"] == "incomplete"
    assert data["exhibits"][0]["measured_lux_hours"] == "800.000000"
    issue = next(i for i in data["issues"] if i["code"] == "unassigned_reading")
    assert issue["severity"] == "risk"
    assert issue["location"]["gallery_id"] == "G1"
    assert issue["location"]["date"] == "2026-01-01"
    assert issue["location"]["count"] == 1


def test_readings_for_unknown_gallery_are_blocking(client):
    request = _reconcile_request(
        [_gallery()], [_exhibit()], [_placement()],
        _hourly("G1") + _readings("GX", [10], "50"),
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["status"] == "infeasible"
    assert any(i["code"] == "unknown_gallery" and i["severity"] == "error"
               and i["location"]["gallery_id"] == "GX"
               for i in data["issues"])


def test_unknown_placement_reference_is_blocking(client):
    request = _reconcile_request(
        [_gallery()], [_exhibit()], [_placement("OTHER")], _hourly("G1"),
    )
    data = client.post("/exposure-reconcile", json=request).json()
    assert data["status"] == "infeasible"
    assert any(i["code"] == "unknown_exhibit" for i in data["issues"])


# -------------------------------------------------------------- invalid inputs


def test_negative_reading_lux_returns_422(client):
    request = _reconcile_request(
        [_gallery()], [_exhibit()], [_placement()],
        [{"gallery_id": "G1", "timestamp": "2026-01-01T10:00:00Z", "lux": "-5"}],
    )
    response = client.post("/exposure-reconcile", json=request)
    assert response.status_code == 422


def test_naive_reading_timestamp_returns_422(client):
    request = _reconcile_request(
        [_gallery()], [_exhibit()], [_placement()],
        [{"gallery_id": "G1", "timestamp": "2026-01-01T10:00:00", "lux": "50"}],
    )
    response = client.post("/exposure-reconcile", json=request)
    assert response.status_code == 422
    assert "timezone-aware" in str(response.json())


def test_out_of_order_readings_return_422(client):
    readings = _readings("G1", [10, 9], "100")
    request = _reconcile_request(
        [_gallery()], [_exhibit()], [_placement()], readings,
    )
    response = client.post("/exposure-reconcile", json=request)
    assert response.status_code == 422
    assert "strictly increasing" in str(response.json())


def test_duplicate_reading_timestamp_returns_422(client):
    readings = _readings("G1", [10, 10], "100")
    request = _reconcile_request(
        [_gallery()], [_exhibit()], [_placement()], readings,
    )
    response = client.post("/exposure-reconcile", json=request)
    assert response.status_code == 422
    assert "duplicate or out-of-order" in str(response.json())


def test_interleaved_galleries_keep_their_own_ordering(client):
    readings = (
        _readings("G1", [9, 11], "100") + _readings("G2", [10, 12], "200")
    )
    readings.sort(key=lambda r: r["timestamp"])
    galleries = [_gallery("G1"), _gallery("G2")]
    exhibits = [_exhibit("E1"), _exhibit("E2")]
    placements = [_placement("E1", "G1"), _placement("E2", "G2")]
    response = client.post(
        "/exposure-reconcile",
        json=_reconcile_request(galleries, exhibits, placements, readings),
    )
    assert response.status_code == 200


# ------------------------------------------------------------------ meta routes


def test_openapi_lists_reconcile_and_keeps_existing_routes(client):
    spec = client.get("/openapi.json").json()
    assert "/exposure-reconcile" in spec["paths"]
    assert "/dose" in spec["paths"]
    assert "/rotation" in spec["paths"]
    post = spec["paths"]["/exposure-reconcile"]["post"]
    assert post["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/ExposureReconcileRequest")


def test_dose_and_rotation_still_work(client, make_gallery):
    dose = client.post("/dose", json={
        "kind": "dose",
        "horizon_start": "2026-01-01T00:00:00Z",
        "horizon_end": "2026-01-03T00:00:00Z",
        "galleries": [make_gallery()],
        "exhibits": [{"id": "T1", "dose_limit_lux_hours": "10000"}],
        "placements": [{"exhibit_id": "T1", "gallery_id": "G1",
                        "start": "2026-01-01T09:00:00Z",
                        "end": "2026-01-01T17:00:00Z"}],
    })
    assert dose.status_code == 200
    assert dose.json()["exhibits"][0]["planned_dose_lux_hours"] == "800.000000"

    rotation = client.post("/rotation", json={
        "kind": "rotation",
        "horizon_start": "2026-01-01T00:00:00Z",
        "horizon_end": "2026-01-03T00:00:00Z",
        "galleries": [make_gallery()],
        "exhibits": [{"id": "T1", "dose_limit_lux_hours": "10000"}],
        "change_dates": ["2026-01-02T00:00:00Z"],
    })
    assert rotation.status_code == 200
    assert rotation.json()["kind"] == "rotation"
