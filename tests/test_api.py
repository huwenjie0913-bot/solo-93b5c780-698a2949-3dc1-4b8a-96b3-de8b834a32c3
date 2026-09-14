"""End-to-end behaviour of /dose and /rotation via the ASGI app."""
from __future__ import annotations


def _dose_request(galleries, exhibits, placements, *, horizon_days=2,
                  risk_threshold="0.90"):
    return {
        "kind": "dose",
        "horizon_start": "2026-01-01T00:00:00Z",
        "horizon_end": f"2026-01-{1 + horizon_days:02d}T00:00:00Z",
        "galleries": galleries,
        "exhibits": exhibits,
        "placements": placements,
        "risk_threshold": risk_threshold,
    }


def _lit_segment(day, spectrum=None, lux="100"):
    segment = {
        "start": f"2026-01-{day:02d}T09:00:00Z",
        "end": f"2026-01-{day:02d}T17:00:00Z",
        "lux": lux,
    }
    if spectrum is not None:
        segment["spectrum"] = spectrum
    return segment


def _open_gallery(gallery_id, illumination, *, capacity=1, timezone="UTC"):
    return {
        "id": gallery_id,
        "timezone": timezone,
        "capacity": capacity,
        "weekly_open": [
            {"weekday": day, "open_time": "09:00", "close_time": "17:00"}
            for day in range(7)
        ],
        "illumination": illumination,
    }


# --------------------------------------------------------------------------- health


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ------------------------------------------------------- classic lux-hour fallback


def test_classic_lux_hours_unchanged_without_spectra(client, make_gallery):
    gallery = make_gallery()
    request = _dose_request(
        [gallery],
        [{"id": "T1", "material": "textile",
          "dose_limit_lux_hours": "10000",
          "historical_dose_lux_hours": "200"}],
        [{"exhibit_id": "T1", "gallery_id": "G1",
          "start": "2026-01-01T09:00:00Z", "end": "2026-01-01T17:00:00Z"}],
    )
    data = client.post("/dose", json=request).json()
    assert data["status"] == "feasible"
    exhibit = data["exhibits"][0]
    assert exhibit["spectral_mode"] is False
    assert exhibit["planned_dose_lux_hours"] == "800.000000"
    assert exhibit["total_dose_lux_hours"] == "1000.000000"
    assert exhibit["occupancy_ratio"] == "0.100000"
    # spectral-only fields remain null for backward compatibility
    assert exhibit["total_equivalent_damage"] is None
    assert exhibit["equivalent_damage_ratio"] is None
    placement = exhibit["placements"][0]
    assert placement["equivalent_damage"] is None
    assert placement["bands"] == []
    segment = placement["daily"][0]["segments"][0]
    assert segment["spectrum_present"] is False
    assert segment["damage_factor"] is None


def test_classic_over_limit_is_infeasible(client, make_gallery):
    request = _dose_request(
        [make_gallery()],
        [{"id": "T1", "dose_limit_lux_hours": "500"}],
        [{"exhibit_id": "T1", "gallery_id": "G1",
          "start": "2026-01-01T09:00:00Z", "end": "2026-01-01T17:00:00Z"}],
    )
    data = client.post("/dose", json=request).json()
    assert data["status"] == "infeasible"
    assert any(i["code"] == "dose_limit_exceeded" and i["severity"] == "error"
               for i in data["issues"])


# --------------------------------------------------------------- spectral dose path


def _spectral_request(source_spectrum, sensitivity_values=("1", "0.2"),
                      damage_limit="600", source_ws=("400", "700"),
                      sensitivity_ws=("400", "700")):
    gallery = _open_gallery("G1", [_lit_segment(1, source_spectrum)])
    exhibit = {
        "id": "P1", "material": "photograph",
        "dose_limit_lux_hours": "100000",
        "historical_equivalent_damage": "0",
        "sensitivity": {"wavelengths_nm": list(sensitivity_ws),
                        "values": list(sensitivity_values)},
        "equivalent_damage_limit": damage_limit,
    }
    placement = {"exhibit_id": "P1", "gallery_id": "G1",
                 "start": "2026-01-01T09:00:00Z", "end": "2026-01-01T17:00:00Z"}
    return _dose_request([gallery], [exhibit], [placement])


LED_SPECTRUM = {"wavelengths_nm": ["400", "440", "460", "700"],
                "values": ["0.05", "1", "1", "0.05"]}
DAY_SPECTRUM = {"wavelengths_nm": ["400", "700"], "values": ["1", "1"]}


def test_spectral_damage_and_cumulative_ratio(client):
    data = client.post("/dose", json=_spectral_request(DAY_SPECTRUM)).json()
    exhibit = data["exhibits"][0]
    assert exhibit["spectral_mode"] is True
    # factor 0.6 for flat light under S: 1 -> 0.2; 800 lux-h * 0.6 = 480
    assert exhibit["planned_equivalent_damage"] == "480.000000"
    assert exhibit["equivalent_damage_ratio"] == "0.800000"
    assert exhibit["equivalent_damage_remaining_ratio"] == "0.200000"
    assert exhibit["remaining_equivalent_damage"] == "120.000000"


def test_equal_luxhours_led_vs_daylight_distinguished(client):
    led = client.post("/dose", json=_spectral_request(LED_SPECTRUM)).json()
    day = client.post("/dose", json=_spectral_request(DAY_SPECTRUM)).json()
    led_damage = float(led["exhibits"][0]["planned_equivalent_damage"])
    day_damage = float(day["exhibits"][0]["planned_equivalent_damage"])
    # identical 800 lux-hours, but damage differs
    assert led["exhibits"][0]["planned_dose_lux_hours"] == "800.000000"
    assert day["exhibits"][0]["planned_dose_lux_hours"] == "800.000000"
    assert led_damage > day_damage


def test_per_band_contributions_sum_to_total(client):
    data = client.post("/dose", json=_spectral_request(LED_SPECTRUM)).json()
    placement = data["exhibits"][0]["placements"][0]
    total = float(data["exhibits"][0]["planned_equivalent_damage"])
    band_total = sum(float(b["equivalent_damage"]) for b in placement["bands"])
    assert abs(band_total - total) < 1e-4
    fractions = sum(float(b["equivalent_damage_fraction"]) for b in placement["bands"])
    assert abs(fractions - 1.0) < 1e-5
    # segment-level factor and bands are also reported under the day
    segment = placement["daily"][0]["segments"][0]
    assert segment["damage_factor"] is not None
    assert segment["bands"], "segment should report per-band contributions"


def test_historical_damage_counts_toward_limit_ratio(client):
    request = _spectral_request(DAY_SPECTRUM, damage_limit="600")
    request["exhibits"][0]["historical_equivalent_damage"] = "420"
    data = client.post("/dose", json=request).json()
    exhibit = data["exhibits"][0]
    # 420 historical + 480 planned = 900 / 600 -> over the equivalent limit
    assert exhibit["total_equivalent_damage"] == "900.000000"
    assert exhibit["equivalent_damage_ratio"] == "1.500000"
    assert any(i["code"] == "equivalent_damage_limit_exceeded"
               for i in data["issues"])
    assert data["status"] == "infeasible"


def test_equivalent_damage_risk_below_threshold(client):
    request = _spectral_request(DAY_SPECTRUM, damage_limit="550")
    data = client.post("/dose", json=request).json()
    # 480 / 550 = 0.8727 -> under 0.90 threshold, feasible, no risk issue
    assert data["status"] == "feasible"
    assert not any(i["code"] == "equivalent_damage_risk" for i in data["issues"])
    request["risk_threshold"] = "0.80"
    data = client.post("/dose", json=request).json()
    assert any(i["code"] == "spectrum_partial_coverage" for i in data["issues"]) is False
    assert any(i["code"] == "equivalent_damage_risk"
               and i["severity"] == "risk" for i in data["issues"])


def test_coverage_gap_issue_structures_missing_ranges(client):
    gapped_source = {"wavelengths_nm": ["300", "500"], "values": ["1", "1"]}
    data = client.post(
        "/dose", json=_spectral_request(gapped_source, source_ws=("300", "500"))
    ).json()
    gap_issues = [i for i in data["issues"] if i["code"] == "spectrum_coverage_gap"]
    assert len(gap_issues) == 2
    located = [
        gap["location"]["missing_wavelength_ranges_nm"][0]
        for gap in gap_issues
    ]
    pairs = {(item["start_nm"], item["end_nm"], item["missing_curve"])
             for item in located}
    assert ("300", "400", "sensitivity") in pairs
    assert ("500", "700", "source") in pairs
    for gap in gap_issues:
        assert gap["location"]["common_wavelength_range_nm"] == ["400", "500"]
    partial = next(i for i in data["issues"]
                   if i["code"] == "spectrum_partial_coverage")
    assert "50.0%" in partial["message"]


def test_no_overlap_spectrum_is_blocking_error(client):
    uv_only = {"wavelengths_nm": ["300", "350"], "values": ["1", "1"]}
    data = client.post(
        "/dose", json=_spectral_request(uv_only, source_ws=("300", "350"))
    ).json()
    issue = next(i for i in data["issues"] if i["code"] == "spectrum_no_overlap")
    assert issue["severity"] == "error"
    assert issue["blocking_constraint"] == "spectral_action_function"
    assert data["status"] == "infeasible"


def test_source_spectrum_without_sensitivity_falls_back_with_risk(client):
    request = _spectral_request(DAY_SPECTRUM)
    # strip the sensitivity pair -> classic exhibit, source spectrum present
    exhibit = request["exhibits"][0]
    exhibit.pop("sensitivity")
    exhibit.pop("equivalent_damage_limit")
    exhibit["dose_limit_lux_hours"] = "100000"
    data = client.post("/dose", json=request).json()
    assert any(i["code"] == "sensitivity_missing" and i["severity"] == "risk"
               for i in data["issues"])
    assert data["exhibits"][0]["spectral_mode"] is False


def test_sensitivity_without_source_segment_falls_back_with_risk(client):
    request = _spectral_request(None)
    request["galleries"][0]["illumination"] = [_lit_segment(1)]
    data = client.post("/dose", json=request).json()
    assert any(i["code"] == "source_spectrum_missing" and i["severity"] == "risk"
               for i in data["issues"])
    assert data["exhibits"][0]["spectral_mode"] is False


def test_duplicate_wavelength_returns_422(client):
    bad = _spectral_request(
        {"wavelengths_nm": ["400", "400", "700"], "values": ["1", "1", "1"]},
        source_ws=("400", "400", "700"),
    )
    response = client.post("/dose", json=bad)
    assert response.status_code == 422
    assert "strictly increasing" in str(response.json())


def test_negative_spectrum_value_returns_422(client):
    bad = _spectral_request(
        {"wavelengths_nm": ["400", "700"], "values": ["-1", "1"]},
    )
    response = client.post("/dose", json=bad)
    assert response.status_code == 422


def test_unknown_reference_is_blocking(client, make_gallery):
    request = _dose_request(
        [make_gallery()],
        [{"id": "T1", "dose_limit_lux_hours": "10000"}],
        [{"exhibit_id": "OTHER", "gallery_id": "G1",
          "start": "2026-01-01T09:00:00Z", "end": "2026-01-01T17:00:00Z"}],
    )
    data = client.post("/dose", json=request).json()
    assert data["status"] == "infeasible"
    assert any(i["code"] == "unknown_exhibit" for i in data["issues"])
