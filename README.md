# Driver Classification Platform

An offline platform for logging vehicle OBD-II data into labeled, per-driver
sessions, and preparing that data for a machine-learning model that classifies
**who is driving** from driving patterns.

Everything runs locally on a single device — no cloud, no remote server. Data is
stored as plain files on disk.

## What it does

1. **Profiles** — create a profile for each driver.
2. **Sessions** — log OBD-II data from a drive into a session, tagged with the
   driver. This tagging is what produces labeled data for training.
3. **Storage** — parse raw OBD-II logs, preserve them immutably, and derive clean,
   evenly-sampled tables suitable for analysis and modeling.
4. **Export** — pull selected drivers, signals, and sessions into a training-ready
   format for the ML model.

The machine-learning goal is to identify the driver from a window of driving
behavior, and to flag drives that don't confidently match any known driver.

## How the data works

The platform ingests **OBD-II logs** (e.g. Car Scanner CSV exports) in a "long"
format — one row per reading, with many parameters (PIDs) interleaved at different
sample rates.

The core signals used for driver classification are:

- **Vehicle speed**
- **Engine RPM**
- **Vehicle acceleration**
- **Throttle / pedal position** (driver-intent signal)

Because each signal is sampled at its own rate and its own timestamps, the raw log
is resampled onto a uniform **10 Hz time grid** (one row per 0.1 s, one column per
signal). Stretches with no real underlying samples are flagged so that interpolated
(fabricated) regions can be excluded from analysis and training.

All parameters from the source log are kept in immutable raw storage; the core
signal set used downstream is a reversible parameter, not a permanent filter.

## Storage layout

Two layers, linked by `session_id`:

- **Metadata** — `SQLite` (a single `.db` file): `drivers` and `sessions` tables,
  including per-session data-quality stats. The driver on each session is the ML
  label; `session_id` is the join key across all storage.
- **Bulk data** — `Parquet` files per session: an immutable raw table (all PIDs) and
  a derived wide table (10 Hz core signals).

```
project/
  platform.db                 # SQLite: drivers + sessions metadata
  sessions/
    session_101/
      raw.parquet             # all PIDs, immutable source of truth
      wide.parquet            # 10 Hz core signals
    ...
```

## Tech stack

- **Language:** Python
- **Parsing / data handling:** pandas, numpy
- **Metadata storage:** SQLite (Python built-in `sqlite3`, serverless, single file)
- **Bulk time-series storage:** Parquet
- **Live acquisition (later):** python-OBD (ELM327 / STN adapters, standard PIDs)
- **Optional local API:** FastAPI
- **UI (later):** local web app or PySide desktop
- **Offline packaging (later):** pinned virtual environment or Docker

## Design principles

- **Offline-first** — all data stays on the local device, stored as files on disk.
- **Labeled by construction** — driver → session → readings, so every reading
  inherits its driver label.
- **Raw is immutable** — source logs are preserved untouched; everything else is
  derived from them.
- **Gaps are flagged, never hidden** — interpolated regions are marked so they can
  be excluded from training.
- **Acquisition behind one interface** — logs and (later) a live adapter share the
  same interface, so the source can be swapped without changing anything downstream.
