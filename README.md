# Museum Light Dose and Rotation API

FastAPI service that schedules light-sensitive museum exhibits against
galleries' illumination calendars. Beyond classic lux-hours accounting, it
supports **spectral action functions**: LED and daylight with identical
lux-hours produce different *equivalent damage* for textiles, photographs and
other dyed materials.

## Running

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# docs at http://127.0.0.1:8000/docs
pytest
```

## Endpoints

| Method | Path        | Purpose |
|--------|-------------|---------|
| GET    | `/health`   | Liveness probe |
| POST   | `/dose`     | Dose for fixed placements (`kind: "dose"`) |
| POST   | `/rotation` | Rotation search (`kind: "rotation"`) |
| POST   | `/exposure-reconcile` | Reconcile measured sensor readings against the plan (`kind: "exposure_reconcile"`) |

## Spectral inputs

* `galleries[].illumination[].spectrum` — source relative-power samples:
  `{"wavelengths_nm": [...], "values": [...]}`. Wavelengths in nanometres,
  strictly increasing, values non-negative; at least two samples.
* `exhibits[].sensitivity` — the material's damage-action curve, same shape.
* `exhibits[].equivalent_damage_limit` — cumulative equivalent-damage ceiling
  (must be paired with `sensitivity`).
* `exhibits[].historical_equivalent_damage` — pre-horizon accumulated damage.

Pydantic rejects duplicate wavelengths, out-of-order/negative wavelengths,
negative values and unpaired sensitivity/limit (HTTP 422).

## Equivalent-damage computation

1. The source spectrum is normalized so its integral over its own span is 1
   (relative units therefore cancel).
2. Both curves are linearly interpolated on the union wavelength grid
   restricted to the **common wavelength range**.
3. `factor = ∫ P_norm(λ) · S(λ) dλ`; a flat sensitivity `S = 1` anchors the
   units so that `factor = 1` and equivalent damage numerically equals the
   lux-hours.
4. `equivalent_damage = factor × lux_hours`, accumulated per segment, per day,
   per placement and per exhibit, including the cumulative fraction of the
   limit (`equivalent_damage_ratio`).
5. Per-band contributions (`ultraviolet`, `violet_blue`, `green`, `red`,
   `near_infrared`) are reported on every segment and placement; band
   fractions partition the integral.

Wavelength ranges covered by only one curve are **not extrapolated**. They
are reported as structured `spectrum_coverage_gap` risk issues whose
`location.missing_wavelength_ranges_nm` lists every missing range with the
curve that lacks it, alongside the common range and covered fractions.
Curves with no overlap produce a blocking `spectrum_no_overlap` error;
zero-area curves produce `spectrum_zero_power`/`spectrum_zero_sensitivity`.

## Exposure reconciliation

`POST /exposure-reconcile` replaces plan-based dose estimates with measured
ones. Dimming, drawn shades and temporary closures make the real exposure
deviate from the illumination calendar, so conservation staff submit the
galleries' sensor logs alongside the galleries, exhibits and placements:

* `readings[]` — `{gallery_id, timestamp, lux}` samples. Timestamps must be
  timezone-aware and lux non-negative (HTTP 422 otherwise). Within each
  gallery the timestamps must be strictly increasing: duplicate or
  out-of-order readings are rejected with HTTP 422.
* `max_sampling_gap_hours` (default `1`) — the longest interval between
  adjacent readings that is still integrated.

Adjacent readings inside a placement are integrated with the trapezoidal
rule. Intervals longer than `max_sampling_gap_hours` — including the edges
before the first and after the last reading — are treated as missing data:
they are **cut out of the integral, never interpolated across**, and
reported as `sampling_gap` risk issues. Readings that fall outside every
placement of their gallery are reported as aggregated `unassigned_reading`
risk issues (per gallery and local day); readings for unknown galleries are
blocking `unknown_gallery` errors.

Per exhibit the response returns `measured_lux_hours`,
`planned_dose_lux_hours`, their `delta_lux_hours`, the `coverage_ratio`
(integrated time ÷ placement time) and the `corrected_remaining_lux_hours`
(`limit − historical − measured`), plus a per-day breakdown per placement.
Limit checks run on the **measured** cumulative dose: over-limit produces
`dose_limit_exceeded` errors, near-threshold occupancy produces
`dose_occupancy_risk` risks. With spectra and a sensitivity curve, each
integrated piece is charged the damage factor of the illumination segment
covering it (lux-weighted when segments overlap; 1:1 fallback plus a
`measured_outside_illumination` risk outside every segment), so
`measured_equivalent_damage` and `corrected_remaining_equivalent_damage`
reflect the measured light. The status is `infeasible` on any error,
`incomplete` when sampling gaps or unassigned readings remain, `feasible`
otherwise.

## Rotation search

Candidate blocks are enumerated between change instants, scored by the
illumination engine, and filtered by the remaining-dose margin. Spectral
exhibits use the **equivalent-damage margin** (a block is admitted only when
`historical + block damage ≤ equivalent_damage_limit`); exhibits without a
sensitivity curve use the **lux-hour margin** (`historical lux-hours + block
lux-hours ≤ dose_limit_lux_hours`). The same rule is applied to cumulative
multi-block paths. Rejections are surfaced as `equivalent_damage_margin` /
`lux_hours_margin` risk issues and counted in `search.margin_rejections`;
`search.filter` names the rule actually applied (`lux_hours_margin` for a
fully classic request, `equivalent_damage_margin` otherwise), and
`search.filters` gives the per-exhibit rule for mixed requests. A
most-constrained-first backtracking assignment then honours gallery capacity
and minimum rest gaps. Exhibits that cannot be placed appear in `unplaced`
with the best rejected candidate's lux-hours, illuminated hours and
equivalent damage.

## Backward compatibility

When neither the segment spectrum nor the exhibit sensitivity is supplied,
the service behaves exactly as before (lux-hours only), all spectral response
fields are `null`/empty, and classic rotation filters on lux-hours. Mixing
(e.g. a spectrum on the segment but no sensitivity curve) falls back to
lux-hours and raises a `sensitivity_missing` / `source_spectrum_missing`
risk issue. When one placement overlaps a mix of spectral and non-spectral
segments, the segments lacking a spectrum are charged equivalent damage
equal to their lux-hours (legacy factor 1, `damage_factor` reported as
`null`), so cumulative damage, limit ratios and feasibility stay consistent
with the total light exposure.
