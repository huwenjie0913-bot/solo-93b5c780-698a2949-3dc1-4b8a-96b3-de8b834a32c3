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

## Rotation search

Candidate blocks are enumerated between change instants, scored by the
illumination engine, and filtered by **equivalent-damage margin**: a block is
admitted only when `historical + block damage ≤ equivalent_damage_limit`.
Rejections are surfaced as `equivalent_damage_margin` risk issues and counted
in `search.margin_rejections`; `search.filter` names the active filter. A
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
risk issue.
