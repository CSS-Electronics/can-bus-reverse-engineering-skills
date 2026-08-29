# Project: Driver Classification Platform (offline)

## Why this project exists

An **offline** platform (no cloud — everything runs locally) where users:
1. Create driver **profiles**.
2. Log **OBD-II data** from a vehicle into labeled **sessions**.
3. Later, an **ML model** classifies *who is driving* from driving patterns.

Build order: **platform first, ML later.** The platform's real job is to produce
*labeled* training data. The profile → session → readings link is what gives every
reading a driver label for free, so that linkage is the backbone of the whole design.

## Status

**Built** (`driver_platform/`):
- Parser (`parser.py`): `parse_log()` reads the raw "long" CSV into a tidy long
  DataFrame; `to_wide(df, signals, hz, gap_threshold)` pivots selected signals onto a
  10 Hz grid, per-signal interpolation method, and a per-signal `is_gap_<column>` flag
  for raw-sample gaps wider than `gap_threshold` (default 5s) — flagged, not NaN'd or
  dropped, so the interpolated values are still there to inspect.
- Validated against the reference drive (`validate.py`): speed/RPM/accel look correct
  (no negative speed, no flatlining). ~25% of the drive falls inside raw-sample gaps
  that get bridged by a straight line — see the corrected throttle rates below, and
  `is_gap_*` for how gaps are surfaced instead of hidden.
- Throttle is parsed into raw storage like every other PID but **excluded from the
  default `CORE_SIGNALS`/wide table** this session — see the corrected rates below.
  `THROTTLE_SIGNALS` in `parser.py` holds all three candidates for a one-line re-add.
- SQLite data model (`db.py`, `schema.sql`): `drivers` and `sessions` tables, linked by
  `driver_id`; `sessions.parquet_path` is reserved for the Parquet store below.

**Not built yet:** Parquet per-session readings storage (next — it's what
`sessions.parquet_path` is waiting for), the `Replayer` acquisition interface, UI, ML
model.

## The data file

A **Car Scanner** OBD-II export. Real format gotchas — handle all of these:

- **Delimiter is `;`** (semicolon), not comma.
- **"Long" format**: one row per single reading, PIDs interleaved. Columns are
  `SECONDS ; PID ; VALUE ; UNITS ; LATITUDE ; LONGTITUDE`.
- The longitude header is **misspelled `LONGTITUDE`** in the file — match the
  literal header, don't assume `LONGITUDE`.
- There is a **trailing `;`** on every row, producing an empty 7th column. Ignore it.
- **All fields are quoted** (`"69590.49"`, `"Engine RPM"`, `"651"`, ...).
- Line endings are **`\r\n`**.
- `SECONDS` is a running clock in seconds (float), NOT a wall-clock time. Use it
  as the relative time axis. Session start = min(SECONDS).
- GPS lat/lon are stamped on every row (useful later, not core to classification).

Sample rows (after the header):
```
"69590.49";"Engine RPM";"651";"rpm";"47.5469";"-122.2307";
"69590.49";"Vehicle speed";"0";"mph";"47.5469";"-122.2307";
"69590.51";"Speed (GPS)";"0.14";"mph";"47.5469";"-122.2307";
```

The reference file (~17 min drive, single driver, 120 distinct PIDs) used during
development was `2026-08-16 19-19-39.csv`, kept locally and gitignored (has GPS
coordinates) — not committed to the repo. Point the parser at your own local copy.

## Core signals (start with these only — not all 120 PIDs)

Match on the **exact** PID string.

| PID string (exact)            | Rate    | Unit | Role                                  |
|-------------------------------|---------|------|---------------------------------------|
| `Vehicle speed`               | ~9.8 Hz | mph  | Primary behavior signal               |
| `Engine RPM`                  | ~9.8 Hz | rpm  | Primary behavior signal               |
| `Vehicle acceleration`        | ~9.6 Hz | g    | Primary behavior signal               |

Speed/RPM/acceleration at ~10 Hz is fast enough to capture the acceleration and
braking transients that distinguish drivers. Keep the other ~117 PIDs (including
throttle, below) available in raw storage but out of the core wide table.

**Throttle — excluded from `CORE_SIGNALS` for now.** The original estimates below
(measured from PID metadata, not the actual reference file) were badly wrong — real
measured rates in the reference drive:

| PID string (exact)            | Actual rate (reference file)      | Unit | Verdict            |
|-------------------------------|------------------------------------|------|---------------------|
| `Absolute pedal position E`   | ~0.02 Hz — 19 samples in one ~4s burst near the end | % | Unusable as-is |
| `Relative throttle position`  | ~0.02 Hz — 17 samples total        | %    | Unusable as-is      |
| `Throttle position`           | ~0.2 Hz — 203 samples              | %    | Least-bad, still sparse |

None are trustworthy enough to resample without misleading long flat/held stretches.
Revisit once throttle is available via CAN. `THROTTLE_SIGNALS` in `parser.py` has all
three ready to re-add to `CORE_SIGNALS` in one line when that changes.

## Resampling rules (long → wide)

Each PID samples at its own rate and its own timestamps; no two signals share a
timestamp. So:
1. Build a uniform time grid at **10 Hz** (every 0.1 s) from session start to end.
2. For each core signal, place a value on every grid point using one of:
   - **Hold last value** (step): reuse the most recent reading before the grid time.
     Safer for discrete/jumpy signals (gear, RPM if noisy).
   - **Linear interpolation**: estimate between the two surrounding readings.
     Better for smooth continuous signals (speed, acceleration).
3. Result: one row per grid time, one column per core signal, no empty cells.

Make the interpolation method a **per-signal choice** (a parameter), so it's easy
to switch. Validate by plotting speed / RPM over the drive — if speed goes negative
or RPM flatlines, the parse or resample is wrong. Also check raw-sample gaps (see
`is_gap_*`, above) — a long gap bridged by linear interpolation can look like a
smooth, plausible acceleration curve that was never actually recorded.

## Architecture (the bigger picture — keep the parser compatible with this)

- **Acquisition is behind one interface.** A `Replayer` streams existing logs, and a
  future live `python-OBD` adapter will implement the *same* interface. Everything
  downstream is built against the interface, so swapping replay → live later is a
  one-line change. Build the parser/replayer with this seam in mind.
- **Storage / data model:**
  - `SQLite` for driver profiles and session metadata (small, relational).
  - `Parquet` per session for readings (bulk time-series). Store raw long readings;
    generate the resampled wide table on export.
  - Link everything by **session_id**; each session carries a **driver_id** (the label).
- **ML export** (later): "these drivers, these signals, resampled to a fixed grid" →
  Parquet/CSV. The 10 Hz wide-table code built now IS the heart of this export.

## Tech stack

- **Python** throughout (same language as the future ML work).
- Parsing/resampling: **pandas** (and numpy).
- Live acquisition later: **python-OBD** (handles ELM327/STN adapters, standard PIDs,
  decoding). Not needed for the parser.
- Metadata: **SQLite**. Bulk data: **Parquet**.
- Optional local service: **FastAPI**. UI later: local web app or PySide desktop.
- Offline packaging later: pinned venv or Docker; `systemd` if it auto-starts in-car.

## Conventions

- Keep functions small and reusable: `parse_log()`, `to_wide(df, signals, hz, method)`, etc.
- Never assume PIDs are simultaneous — always resample onto an explicit grid.
- Preserve raw data immutably; derive the wide table, don't overwrite.
- Prefer explicit, benchmarked behavior over assumptions (e.g. measure achieved rates
  rather than trusting nominal ones).
- Single session = validates the pipeline only. Real classification needs multiple
  drivers across multiple sessions; the pipeline must scale to that without changes.
