"""Calendar/scheduler edge cases: timezones, closures, capacity, rest."""
from __future__ import annotations

import pytest


def _request(gal, exhibits, placements, *, horizon_end="2026-01-03T00:00:00Z"):
    return {
        "kind": "dose",
        "horizon_start": "2026-01-01T00:00:00Z",
        "horizon_end": horizon_end,
        "galleries": [gal],
        "exhibits": exhibits,
        "placements": placements,
    }


def test_local_timezone_day_splitting(client):
    # Shanghai (UTC+8): an 11:00Z segment is 19:00 local -> lands Jan 1 local,
    # and a 15:00Z end spans a local midnight.
    gal = {
        "id": "SH",
        "timezone": "Asia/Shanghai",
        "capacity": 1,
        "weekly_open": [
            {"weekday": day, "open_time": "09:00", "close_time": "17:00"}
            for day in range(7)
        ],
        "illumination": [
            {"start": "2026-01-01T10:00:00Z", "end": "2026-01-01T12:00:00Z",
             "lux": "100"},
            {"start": "2026-01-01T16:00:00Z", "end": "2026-01-01T18:00:00Z",
             "lux": "100"},
        ],
    }
    request = _request(
        gal,
        [{"id": "T1", "dose_limit_lux_hours": "10000"}],
        [{"exhibit_id": "T1", "gallery_id": "SH",
          "start": "2026-01-01T09:00:00Z", "end": "2026-01-01T19:00:00Z"}],
    )
    data = client.post("/dose", json=request).json()
    assert data["status"] == "feasible"
    daily = data["exhibits"][0]["placements"][0]["daily"]
    # 10-12Z (18-20 local, Jan 1) + 16-18Z (00-02 local, Jan 2) -> 2 days
    dates = {day["gallery_date"] for day in daily}
    assert dates == {"2026-01-01", "2026-01-02"}
    total = sum(float(day["lux_hours"]) for day in daily)
    assert abs(total - 400.0) < 1e-6


def test_closed_exception_removes_open_hours(client, make_gallery):
    gal = make_gallery()
    gal["closed_exceptions"] = [
        {"name": "power outage",
         "start": "2026-01-01T09:00:00Z", "end": "2026-01-01T13:00:00Z",
         "expected_open": True},
    ]
    request = _request(
        gal,
        [{"id": "T1", "dose_limit_lux_hours": "10000"}],
        [{"exhibit_id": "T1", "gallery_id": "G1",
          "start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"}],
    )
    data = client.post("/dose", json=request).json()
    placement = data["exhibits"][0]["placements"][0]
    # 8h opening minus 4h closure
    assert float(placement["open_hours"]) == pytest.approx(4.0, abs=1e-3)


def test_invalid_timezone_emits_risk_and_uses_utc(client):
    gal = {
        "id": "BAD", "timezone": "Mars/Olympus", "capacity": 1,
        "weekly_open": [
            {"weekday": day, "open_time": "09:00", "close_time": "17:00"}
            for day in range(7)
        ],
        "illumination": [
            {"start": "2026-01-01T09:00:00Z", "end": "2026-01-01T17:00:00Z",
             "lux": "100"},
        ],
    }
    request = _request(
        gal,
        [{"id": "T1", "dose_limit_lux_hours": "10000"}],
        [{"exhibit_id": "T1", "gallery_id": "BAD",
          "start": "2026-01-01T09:00:00Z", "end": "2026-01-01T17:00:00Z"}],
    )
    data = client.post("/dose", json=request).json()
    assert any(i["code"] == "invalid_timezone" and i["severity"] == "risk"
               for i in data["issues"])
    assert data["exhibits"][0]["planned_dose_lux_hours"] == "800.000000"


def test_unknown_candidate_gallery_blocks_rotation(client):
    request = {
        "kind": "rotation",
        "horizon_start": "2026-01-01T00:00:00Z",
        "horizon_end": "2026-01-03T00:00:00Z",
        "change_dates": ["2026-01-01T09:00:00Z"],
        "galleries": [{
            "id": "G1", "capacity": 1,
            "weekly_open": [
                {"weekday": day, "open_time": "09:00", "close_time": "17:00"}
                for day in range(7)],
            "illumination": [
                {"start": "2026-01-01T09:00:00Z", "end": "2026-01-01T17:00:00Z",
                 "lux": "100"}],
        }],
        "exhibits": [{
            "id": "T1", "dose_limit_lux_hours": "10000",
            "candidate_gallery_ids": ["GHOST"],
        }],
    }
    data = client.post("/rotation", json=request).json()
    assert data["status"] == "infeasible"
    assert any(i["code"] == "unknown_candidate_gallery" and i["severity"] == "error"
               for i in data["issues"])
    assert data["unplaced"][0]["candidate_count"] == 0


def test_minimum_rest_gap_validated_for_dose(client, make_gallery):
    gal = make_gallery(illumination=[
        {"start": "2026-01-01T09:00:00Z", "end": "2026-01-01T17:00:00Z", "lux": "100"},
        {"start": "2026-01-02T09:00:00Z", "end": "2026-01-02T17:00:00Z", "lux": "100"},
    ])
    request = _request(
        gal,
        [{"id": "T1", "dose_limit_lux_hours": "10000",
          "minimum_rest_hours": "20"}],
        [
            {"exhibit_id": "T1", "gallery_id": "G1",
             "start": "2026-01-01T09:00:00Z", "end": "2026-01-01T17:00:00Z"},
            {"exhibit_id": "T1", "gallery_id": "G1",
             "start": "2026-01-02T09:00:00Z", "end": "2026-01-02T17:00:00Z"},
        ],
    )
    data = client.post("/dose", json=request).json()
    issue = next(i for i in data["issues"] if i["code"] == "minimum_rest_not_met")
    # gap from Jan 1 17:00 to Jan 2 09:00 = 16h < required 20h
    assert issue["value"] == "16.0000"
    exhibit = data["exhibits"][0]
    assert exhibit["minimum_rest_gap_hours"] == "16.0000"
