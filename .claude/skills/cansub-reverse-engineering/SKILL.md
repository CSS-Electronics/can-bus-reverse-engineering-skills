---
name: cansub-reverse-engineering
description: >
  Reverse engineer / decode CAN bus signals: find which CAN ID, byte and bit
  encode a real-world quantity (vehicle speed, engine RPM, state-of-charge,
  coolant temp, throttle, door lock, ...) and derive its start-bit, length,
  endianness, scale and offset into a DBC. A deterministic chain of Python scripts
  (survey -> correlate -> bitsearch -> build_dbc -> verify) replaces "a human
  watches the screen to spot correlations". Works three ways: (1) OFFLINE - decode a
  signal from an EXISTING recorded log the user already has (a CAN + OBD2 log, an
  MF4/CANedge log, or a webCAN CSV) using a separately decodable reference such as
  an OBD2 PID (engine RPM, vehicle speed, coolant temp) or a CSS Electronics GPS
  source (CANmod.gps, or a CANedge's internal GPS/IMU on CAN9 - bundled DBCs, e.g.
  GPS speed as a reference for proprietary vehicle speed) - NO hardware needed;
  (2) LIVE - capture from a CSS Electronics CANsub interface with a human-supplied
  reference; or (3) VISION - the user has a CAN log plus a VIDEO of a display
  (dashboard / gauge app / instrument) showing the true value, and a local
  open-source OCR script digitizes the on-screen number into the reference. Trigger
  this skill whenever the user wants to reverse engineer, decode, identify or "find
  the CAN ID/bits for" an unknown signal, or build a DBC from a log - INCLUDING
  phrasings like "reverse engineer Vehicle Speed from my CAN/OBD2 log file
  <name>.csv", "which CAN message carries RPM in this log", or "decode <signal> from
  my CAN log using this dashboard video as the reference".
  Targets plain, non-multiplexed CAN signals. Ad-hoc input logs the user refers to
  by filename usually sit in the working-directory root (or under TEMP/) — glob for
  the named file there before asking where it is.
---

# CANsub reverse engineering

A deterministic, scriptable workflow for reverse engineering CAN signals with a
CANsub. The bundled scripts in `scripts/` each cover one stage. The scripts never
transmit **data** frames onto the bus. By default, though, `capture.py` connects
in **normal mode** so the CANsub *acknowledges* the frames it receives (the ACK
bit). This is required for **single-node sensor-to-CAN modules**: with no ACK, a
lone transmitting node hits ACK errors, gets stuck retransmitting, and its values
won't update cleanly. On a multi-node bus (where other nodes supply the ACK) pass
`capture.py --listen-only` to keep the CANsub fully silent. Bit-rate
auto-detection (`detect_bus.py`) is always passive/`listen_only` — you can't ACK
while cycling candidate rates — so ACK only begins on the capture connection,
after the rate is known. Human timing/value input is logged to a separate
*sidecar* file and aligned to the trace by epoch timestamp (the CANsub sets its
clock to host time on connect, so trace and sidecar share a reference).

## Scope (v1)

- Targets **plain, non-multiplexed CAN signals** (the signal being decoded). No
  multiplexor decoding, no J1939-PGN logic, no ISO-TP / TP reassembly for the
  *target* (deferred). A *multiplexed reference DBC* (e.g. OBD2) is fine — see the
  **Offline workflow** — because cantools decodes the reference; the raw target is
  still treated as a plain field.
- Three reference sources: a **live** human reference via the CANsub + Flask app (the
  Workflow below); when the user already has a recorded log with a decodable
  reference, **decode the reference from the log** (Offline workflow); or, when the
  user has a recorded log plus a **video of a display** showing the value,
  **OCR the reference from the video** (Vision workflow). All three feed the same
  correlate → bitsearch → build_dbc → verify pipeline.
- Handles **classical CAN and CAN FD** payloads (up to 64 bytes); fields are
  extracted with arbitrary-precision integers, so a signal can sit anywhere in an
  FD frame. (The generated DBC decodes FD correctly via the message length; it
  does not emit the DBC `VFrameFormat` FD attribute.)
- Fields may be **bit-packed** (sub-byte, off byte boundaries, behind a leading
  flag bit) — common in compact sensor frames. `survey.py` detects this and
  proposes a field map; `bitsearch.py` finds the exact field. Don't assume
  byte alignment.
- `survey.py` flags **rolling counters** (excluded from correlation) and
  **checksum-like** bytes (a hint — not excluded, since a wide signal's low byte
  can resemble one).
- The bus **bit-rate is unknown** and resolved by probing (`detect_bus.py`).
- **Profile**: choose silent (vehicle) vs ACK (bench) at `detect_bus.py`; an init
  health-check warns on an unhealthy bus. See `references/re-methodology.md`.

## Output structure — temporary vs decoded deliverables

At the **start of every RE exercise**, establish two names from the user's
request (ask if unclear):

- `<application>` — the system/module under test, e.g. `sensor-to-can`.
- `<signal>` — the signal being decoded this round, e.g. `gauge1`.

Use lowercase **kebab-case** for both, and for every DBC **filename**. (DBC
*signal/message* identifiers inside the file can't contain hyphens — use the
plain/underscored form there, e.g. signal `gauge1`.)

**Where outputs go.** All output folders are created at the **root of your
working directory** (the cwd you launched from — the same place `.venv/` lives),
**never inside the skill**. The scripts write these paths *relative to the cwd*, so
the rule is simply: **always run the commands from your working-directory root** (see
the **Workflow** note on how `scripts/` resolves). Do not `cd` into the skill folder
and do not prefix output paths with the skill path — that is what puts deliverables
inside the skill by mistake.

Route outputs by lifetime:

- **`temp-output/`** — transient working files, safe to delete: `bus.json`,
  `trace_*.csv`, `sidecar_*.csv`, `survey_*.json`, `candidates_*.json`,
  `bitsearch_*.json`. `capture.py`/`flask_sync.py`/`survey.py`/`correlate.py`/
  `bitsearch.py` write here by default — leave them as-is.
- **`decoding-output/<application>/<signal>/`** — the kept deliverables for a
  confirmed signal: the single-signal DBC `<signal>.dbc` and the verify plot.
  Pass `build_dbc.py --out decoding-output/<application>/<signal>/<signal>.dbc`
  and `verify.py --png decoding-output/<application>/<signal>/<signal>.png`.
- **`decoding-output/<application>/<signal>/analysis-plots/`** — per-step
  visualizations auto-emitted by survey / correlate / bitsearch / build_dbc.
  Filenames are short, step-numbered, and fixed (the signal/application context is
  already in the folder path) so they sort in workflow order: `1-survey-bus-activity.png`
  (bus bit-activity heatmap; in the live case use `--suffix steady`/`--suffix sweep`
  for the baseline vs exercised scans, giving `1-survey-bus-activity-steady.png` and
  `…-sweep.png`), `2-correlation.png` (ID×byte correlation heatmap),
  `3-bitsearch-grid.png` (start-bit×length R² grid), `3b-resolution-refine.png` (only
  when the LSB resolution was grown from transition data: narrow staircase vs refined
  ramp), `3c-bit-cascade.png` (per-bit flip-rate bars showing the resolution cascade,
  the exercised field shaded and the inferred byte-aligned extent tinted),
  `4-fit-diagnostic.png` (fit diagnostic); the offline reference-confirmation
  plot is `0-decode-reference.png`.
  Pass each step `--plots-dir
  decoding-output/<application>/<signal>/analysis-plots/` so the whole
  investigation's plots collect here (blog-ready; they also help spot extra
  candidate signals). On by default; `--no-plots` to skip. Without `--plots-dir`
  they fall back to `temp-output/analysis-plots/`.
- **`decoding-output/<application>/<application>.dbc`** — the combined,
  application-level DBC across all confirmed signals. Don't hand-merge: use the
  **`combine-dbc`** skill, which scans `decoding-output/<application>/*/*.dbc`
  and merges them into it (re-runnable at any time as signals accumulate).

`TEMP/` is one of the user's ad-hoc *input* folders — read inputs from there if
asked, never write to it. **Finding a log the user names:** when the request
mentions a file by name (e.g. `chevy-tahoe-obd2-can-data.csv`) or "my log/CSV",
immediately glob for it — check the **working-directory root first** (where users
usually drop the file), then **`TEMP/` recursively** — rather than asking the user
where it is. Only ask if the glob finds nothing or several plausible matches.

## Signal taxonomy → drives the workflow

Classify the target along two axes (see `references/re-methodology.md`):

- **Discrete** (door lock, gear, wiper state) vs **continuous** (speed, SoC, RPM).
- **Stationary** (reproducible on a bench / while parked) vs **driving** (needs
  real operation).

This picks the **reference source** and the **excitation** you ask the user to
perform. There are two reference sources, and which you use depends on whether a
machine reference exists, NOT on the signal being "dynamic":

**A. Live human reference (the flask app — two excitation modes).** Launch
`flask_sync.py` (the "CANsub Reference Generator") to digitize human input whenever you
do NOT have a separately-decodable machine reference. The app has a **Holds | Sweep**
toggle (`--mode both`, default); each Start records a new numbered run **tagged**
holds/sweep in the runs index, so you capture **both** kinds and use each for what it is
good at:

- **HOLDS (anchors) → CALIBRATION.** A list of known values as states; the user SETTLES
  the signal at one, then clicks it (or presses its digit). Each click records a FIXED
  window (default 2 s, `--window`) as that value (a `kind=anchor` tag + dense
  `kind=value` rows). Windows-only: the transitions between holds are unlabelled (settle
  BEFORE clicking). The clean held levels are calibration anchors — use this run to fit
  scale/offset (`build_dbc` / `calibrate`) and to verify absolute levels.
- **SWEEP (slider) → IDENTIFICATION + RESOLUTION.** A vertical slider spanning
  `[--min,--max]`; the user drags it to track the signal smoothly across its whole range.
  The slider value is logged densely (~20 Hz) as `kind=value` rows with **no anchors**.
  Use this run to FIND the field: continuous variation breaks the correlate **degeneracy**
  that a few discrete holds cause (on stepped holds, counters tie at ~1.0; on a smooth
  ramp the true field scores ~1.0 and counters ~0), and the transitions let bitsearch's
  **resolution-refinement** recover the FULL field width (discrete holds under-read it as
  a narrow high-bit slice). It can be laggy/noisy — it only fixes WHICH field and HOW
  WIDE; absolute scale comes from the holds run.

**Which modes per signal:**
- **Continuous** (a gauge you can set; speed/RPM; an accelerometer through gravity): do
  BOTH — a Sweep run to identify the field, a Holds run at known values to calibrate.
- **Discrete state** (door lock 0/1, gear P/R/N/D, wiper off/low/high): a Holds run on
  those states is enough; `correlate --type continuous` then `bitsearch` finds the bit.
  (A *truly momentary* event you cannot hold is the one exception: headless
  `annotate.py --mode event` + `correlate --type discrete`. The headless equivalent of
  the slider is `annotate.py --mode continuous`.)
- **Continuous + dynamic** (speed/RPM while driving, can't park): the Sweep slider tracks
  the live value as the user drives; add a Holds run by matching a few target levels
  (0/20/40/60/80) for calibration.

**Tell the user the protocol explicitly:** for HOLDS, settle BEFORE clicking (only the
next `--window` s is sampled); for SWEEP, drag smoothly across the full range — a few
slow full sweeps. Steady holds double as calibration anchors.

**B. Offline machine reference (preferred WHEN AVAILABLE, but often it is not).** If
a separately-decodable CAN reference for the signal already exists on the bus (an
OBD2 PID, a GPS-to-CAN / CANmod / sensor-to-CAN module) in a recorded log, decode
THAT as the reference instead (see the **Offline workflow**). It is machine-decoded
(near-zero lag) so it fits far better than digitized human input. But it requires
such a reference to exist; when none does (the common case for a proprietary
signal), the live human reference (A) is your only option. Do NOT route a dynamic
signal to the offline workflow by default just because it is dynamic.

**`--ref-window` is for the HOLDS run only.** Pass **`--ref-window <seconds>`** to
`correlate` / `bitsearch` / `build_dbc` / `verify` (match the flask `--window`) so they
score/fit ONLY the deliberately-held windows; the full signal is still decoded and
plotted, but transitions never pollute calibration or the gate, and `verify` shades each
window (green = held still, red = a transition leaked in). The **SWEEP run is the
opposite**: analyse it **WITHOUT `--ref-window`** — the dense `kind=value` slider stream
is a *continuous* reference, so identification and resolution-refinement see the whole
ramp. (Same continuous path the Offline workflow uses for a decoded machine reference.)

**Division of labour, and multi-run discipline.** The default live flow is two runs:
**identify the field on the SWEEP run** (`survey --suffix sweep` → `correlate` →
`bitsearch`, no `--ref-window`), then **calibrate on the HOLDS run** (`build_dbc` /
`calibrate`, with `--ref-window`), then `verify` on both. If results stay ambiguous
(close bitsearch scores, low R², `verify` UNCONFIRMED, or plausibility flags), capture
**additional runs with *different* excitation** — a slow sweep, a vigorous full-range
sweep, another axis — and require the same field to win across runs. Each Start records a
new numbered, tagged run pair (below), so this is cheap.

## Environment — run scripts from the project venv

The scripts need third-party Python packages (cantools, numpy, scipy, pandas,
matplotlib, and — for live capture — python-can, python-can-cansub, flask,
requests). They live in a **project-local virtual environment** at `.venv/` at the
**root of your working directory** (the cwd, alongside `temp-output/` and
`decoding-output/`), built from `requirements.txt`. **Do not rely on a global
Python.**

**In every command in this skill, `python` means the venv interpreter:**
- Windows: `.venv\Scripts\python.exe`
- macOS / Linux: `.venv/bin/python`

Before running any script, confirm `.venv/` exists. **If it does not, stop and ask
the user to run the one-time setup** — do not silently fall back to the system
Python:
- Windows: run `install.bat` from your working-directory root
- macOS / Linux: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`

## CANsub technical reference

For any CANsub hardware, API, or protocol detail needed while running this
workflow — the REST / WebSocket API, bit-timing, hardware filters,
connectors / specs, firmware, certificate install, or `python-can-cansub` usage
— consult the **`cansub-knowledge`** skill in this repo. Its bundled docs are
authoritative; prefer them over training-data recall.

## Workflow

**Run every command from the root of your working directory** (your cwd — where
`.venv/`, `temp-output/`, and `decoding-output/` live), and invoke every script
with the venv interpreter (see **Environment** above). The skill is installed under
`.claude/skills/cansub-reverse-engineering/`, so in the commands below **`scripts/`
is shorthand for `.claude/skills/cansub-reverse-engineering/scripts/`** — write that
full path when you run them (only the *scripts* live under the skill; every *output*
path stays relative to your cwd so it lands at the working-directory root). Do not
`cd` into the skill folder — that would create `temp-output/`/`decoding-output/`
inside the skill.

1. **Init: device + bit-rate + profile + health (always first).**
   `python scripts/detect_bus.py --profile <vehicle|bench>`
   Discovers the CANsub via mDNS, auto-detects the bit-rate passively
   (listen_only + error frames; first valid frame wins), records the **profile**,
   reads the device **bus status** to sanity-check health, and writes
   `temp-output/bus.json`. If detection fails, ask the user and pass `--bitrate`.

   **Choose `--profile` from context (this sets silent vs ACK for all later
   captures):**
   - `vehicle` (**default / safe**) → SILENT (`listen_only`). Use for any existing
     active multi-node bus: car, truck, bike, machine. Other ECUs ACK; the CANsub
     never disturbs the bus. Pick this whenever unsure.
   - `bench` → NORMAL (ACK). Use only for a **single node on a desk** (one ECU /
     sensor module). With no other node to ACK, the CANsub must ACK or the lone
     node hits ACK errors and floods one frame (the classic "10k fps of the same
     payload" failure).

   The health check warns loudly (never blocks) if the bus is bus-off /
   error-passive, idle (`frame_rate=0`), or throwing errors — catching "no data"
   and wrong-profile (no-ACK) situations before you waste a capture. Override the
   recorded mode on any capture with `--listen-only` / `--normal`.

2. **Capture a baseline / working trace.**
   `python scripts/capture.py --duration 15 --label baseline`
   Writes `temp-output/trace_baseline.csv` (webCAN CSV format). Connects in
   normal mode so the CANsub ACKs the sensor (needed for single-node modules);
   add `--listen-only` for fully passive sniffing on a multi-node bus.

3. **Survey the bus (this BASELINE scan maps bus STRUCTURE, not the target).**
   `python scripts/survey.py --trace temp-output/trace_baseline.csv --suffix steady \
       --plots-dir decoding-output/<application>/<signal>/analysis-plots/`
   Per-ID stats (count, cycle time, DLC, latest payload) plus a bit-activity
   "heatmap": which bits/bytes are static vs changing, byte entropy, rolling
   counters. Bit indices are **LSB-first** (the same convention bitsearch uses).
   **survey reports a full per-ID table, a mid-report summary, and a final
   `== N unique IDs surveyed ==` line; the survey JSON (`temp-output/survey_*.json`)
   is the authoritative ID set. Read the ID count from that final line or the JSON —
   never infer it from a `tail`-truncated listing** (the "Proposed field maps" tail is
   only the bit-packed subset, not every ID).
   For a densely **bit-packed** frame it also proposes a heuristic **field map**
   (where sub-byte fields likely start) and flags a constant leading bit (e.g. a
   `valid` flag) that pushes fields off byte boundaries.
   **Be explicit with the user about what this scan is and is not.** In the live
   case the target signal is usually *not moving yet* (the gauge is parked, the car
   is stationary), so its bits look static here — that is expected. This steady scan
   is for **bus structure**: enumerate IDs, cycle times, counters/checksums, and
   which bytes are dynamic from *other* traffic, to shrink the candidate-ID set and
   *seed* bitsearch. You will see the TARGET light up in the **second** survey below,
   on the trace recorded while the user exercises it (step 4b). Do not ask the user
   to manipulate anything for this first scan.
   Emits a **bus bit-activity heatmap** (`1-survey-bus-activity-steady.png`, ID × bit,
   flip-rate shaded, counter/checksum/flag bytes + field-map starts annotated). The
   `--suffix` keeps the steady (baseline) and sweep (exercised) scans as separate files
   so neither overwrites the other.

4. **Capture the reference (the flask app — record BOTH a Sweep and a Holds run).**
   Launch the UI ("CANsub Reference Generator") **fully configured** so the user never
   edits fields in-browser. `--mode both` (default) shows a **Holds | Sweep** toggle;
   pass the unit / range / known values up front. (Comma-leading-with-`-` values need the
   `=` form: `--state-values=-9.81,0,9.81`.)
     - gauge / accelerometer: `python scripts/flask_sync.py --unit m/s^2 --min -9.81 --max 9.81 --state-values=-9.81,0,9.81 [--window 2]`
     - discrete state (e.g. a lock bit): `python scripts/flask_sync.py --unit state --min 0 --max 1 --state-values 0,1`
     - dynamic signal (e.g. speed): `python scripts/flask_sync.py --unit km/h --min 0 --max 120 --state-values 0,20,40,60,80,100`
   Open http://127.0.0.1:5000. For a **continuous** signal record TWO runs:
   **(a) a SWEEP run** — toggle to *Sweep*, Start, drag the slider to track the signal in
   a few slow full sweeps spanning the whole range, Stop; **(b) a HOLDS run** — toggle to
   *Holds*, Start, settle at each known value and click it, Stop. (A **discrete-state**
   signal needs only the Holds run.)
   **Each Start records a NEW numbered run** → `trace_run_<N>.csv` + `sidecar_run_<N>.csv`
   (a consistent pair; nothing overwritten/mixed), **tagged holds/sweep** in
   `runs_run.json` and listed in the UI. **Note which run number is the sweep and which is
   the holds** — you analyse them differently below. Add `--listen-only` for multi-node
   sniffing.
   - **Sweep run** = dense `kind=value`, no anchors → analyse **without `--ref-window`**
     (continuous). Used for identification + resolution.
   - **Holds run** = `kind=anchor` windows → analyse **with `--ref-window <window>`**.
     Used for calibration + absolute verify.
   Headless alternative: `python scripts/capture.py --duration 60 --label run` plus
   `python scripts/annotate.py --mode continuous --label run` (the slider's headless twin —
   keyboard-driven dense `kind=value` + parked-value heartbeat).

4b. **Survey the SWEEP trace (the target lights up) and DIFF against the baseline.**
   `python scripts/survey.py --trace temp-output/trace_run_<sweep>.csv --suffix sweep \
       --baseline temp-output/survey_baseline.json \
       --plots-dir decoding-output/<application>/<signal>/analysis-plots/`
   Re-run the survey on the **sweep** run (recorded while the signal was exercised). With
   `--baseline <the step-3 survey JSON>` it prints **which IDs/bytes became active vs the
   steady scan** — the exercised target stands out. (Counters/pulse inputs can also
   activate, so this NARROWS the field; correlate + bitsearch decide.) **This delta — not
   the calm step-3 scan — is the informative part.** On the calm baseline almost
   everything is static (the parked target included), so don't form a hypothesis from its
   byte patterns: a tidy `8×0x80` is no more "8 signals" than it is "definitely not
   signals" (step 3 now just *notes* the static IDs and warns against theorising from
   them). Let the delta + correlation decide. Emits `1-survey-bus-activity-sweep.png`
   alongside the steady one.

5. **Correlate → a coarse ID shortlist (on the SWEEP run, no `--ref-window`).**
   `python scripts/correlate.py --trace temp-output/trace_run_<sweep>.csv \
       --sidecar temp-output/sidecar_run_<sweep>.csv --type continuous`
   Run identification on the **sweep** run **without `--ref-window`** (the dense slider
   stream is a continuous reference): the smooth ramp makes the true field score ~1.0
   while counters fall to ~0, so the ranking is clean. Treat the byte/width as a *hint* —
   on a bit-packed frame the byte-aligned view can't see the real field; bitsearch is the
   field of record. **Do NOT identify on the holds run:** a few stepped levels make MANY
   candidates (counters included) tie at ~1.0. correlate now flags that degeneracy — a
   `[!] sparse reference` warning and a `Note: N fields across M IDs tie at …` tie-group
   line — and points you to the sweep run. **Never override the reference-backed ranking
   with a visual story about what a frame "looks like"** (e.g. "`8×0x80` = 8 gauges"); a
   uniform-constant frame is almost always zeroed counters, and a fast frame with a
   dithering low byte is a candidate analog signal, not a counter. Emits
   `2-correlation.png` (winner boxed) — scan it for *other* candidates too. Pass
   `--plots-dir …/analysis-plots/`. (A mirror frame — the same signal on a CAN FD and a
   classical ID — legitimately ties this way; either carrier is valid. For a discrete
   state with only a Holds run, add `--ref-window 2`.)

6. **bitsearch → the canonical field (on the SWEEP run; the field of record).**
   `python scripts/bitsearch.py --trace temp-output/trace_run_<sweep>.csv \
       --sidecar temp-output/sidecar_run_<sweep>.csv --id 0x123 --lag 0.2`
   (Lag: `--lag <seed>` + `--lag-refine <±window>` seeds the search from correlate's
   reported lag; for the **video workflow** pass `--max-lag 2` instead — the symmetric
   `--lag 0 --lag-refine 2` equivalent that matches `correlate`/`verify`.)
   Run this on the **sweep** run **without `--ref-window`** so the continuous transitions
   feed **resolution-refinement** — it grows the field LSB-ward to its FULL width (a
   holds-only run under-reads it as a narrow high-bit slice; see the resolution note below).
   Exhaustively scans **start-bit × length × endianness × sign**, ranks by
   lag-aligned linear-R² + reference-free **plausibility** (wrap-free / smooth) +
   Spearman, dedups overlapping reads, restricts boundaries to *active* bits (so it
   never grabs a constant flag/padding bit), and prints a **Decision** line stating
   the winning permutation, the fitted scale (with sign), and why it won. Copy its
   `build_dbc.py …` command. This — not correlate's byte guess — is the field of
   record. (Seed `--min-len/--max-len` from the survey field map for packed frames;
   they bound **both** little- and big-endian widths, default `--max-len 24`.)
   **Over-wide guard:** when two co-varying fields sit side by side (a wheel-speed
   *pair*, an L/R or X/Y axis), reading them together still correlates ~perfectly
   but yields a fragile field with a tiny scale (`true ÷ 256^extra`). bitsearch's
   **parsimony** rule keeps the shortest field that fits equally well, so it now
   reports the true narrow field with its real scale (e.g. 16-bit `1/64 km/h`, not
   a 32-bit read at `2^−22`). If you still suspect an over-wide read, re-run with
   `--max-len 16` and watch for the **R²-vs-width knee** (R² jumps at the field's
   own low byte, then plateaus into the neighbour). bitsearch **auto-masks
   high-confidence sentinels per candidate before scoring** (see the outlier note
   below) so a far-out "signal invalid" code can't wreck a candidate's linear R²
   and hand the win to a coarse slice — the classic failure mode is a
   signal-unavailable sentinel letting a narrow slice beat the true wide field
   until masking recovers the R². It emits a **start-bit×length R²
   grid** (`3-bitsearch-grid.png`, little/big panels + the winner's parsimony knee)
   that visualizes *why* the field won — pass `--plots-dir …/analysis-plots/`.

   **Resolution refinement (the complement of the over-wide guard; matters for
   holds AND noisy live streams).** A field's MSB is reliable (the holds/ramps span
   the range) but its LSB is easily under-read: a coarse high-bit slice and the
   full-width field reproduce the same levels, so parsimony keeps the *shorter* one
   (e.g. a 16-bit gauge decoded as a 5-bit slice). bitsearch widens the winner's LSB
   **reference-free**, by two complementary mechanisms — the wider (lower) LSB wins:
   - **Smoothness/distinct-count growth** — the clean-ramp regime (offline machine
     reference, or holds with clean transitions): grow the LSB while the extra low
     bits make the decode *finer* during motion (distinct-value count rises) and stay
     *smooth* (no added jitter). Stops when a lower bit injects jitter.
   - **Flip-rate cascade growth** — the noisy live-stream regime: a noisy real-time
     analog signal's genuine LSBs **dither** (flip direction frame-to-frame), which
     the smoothness test wrongly rejects as noise — so smoothness alone under-reads
     the width. The cascade instead *expects* fast LSBs: within one little-endian
     field the per-bit flip-rate roughly **doubles each step toward the LSB**, so it
     grows across the contiguous active run, stopping only at a **constant bit** (the
     field's true LSB / sub-resolution boundary) or a flip-rate **jump/drop** (a
     separate, faster field below — a counter, or the low half of an over-wide pair).
     A *confident* cascade boundary is authoritative: it widens past a dither-stalled
     smoothness result AND reins a smoothness result that over-grew into a slow
     neighbour. This is what recovers the FULL width from a noisy sweep (e.g. the
     gauge's `6|7` slice → the true `3|10` exercised field). The **same cascade also
     guards the parsimony swap**, so the wide field is not demoted to a narrow slice
     in the first place when its extra LSBs continue the cascade.

   It prints a `[resolution] grew N|M -> …` line when it fires and emits a
   **`3b-resolution-refine.png`** (narrow staircase vs refined ramp) and a
   **`3c-bit-cascade.png`** (per-bit flip-rate bars with the exercised field shaded and
   the inferred byte-aligned extent tinted) — **show the `3c-` plot** when explaining
   resolution. `--no-resolution-refine` disables it.

   **Exercised vs inferred bits, and `--byte-align`.** The cascade pins the
   **exercised** field — the bits that actually moved — as the *firm* result. The bits
   below it (always 0 → sub-resolution, the source quantizes) and above it (constant →
   the range wasn't fully exercised) cannot be proven from the data, but a real OEM
   field is usually byte/word-aligned and wider. bitsearch therefore **reports** the
   byte-aligned field that *encloses* the exercised one across constant bits (e.g.
   `3|10` → `0|16`, reproducing the canonical 16-bit definition) and warns which high
   bits were never exercised (→ recommend a fuller-range sweep) and which low bits are
   sub-resolution. It does **not** apply that snap by default — it rewrites the reported
   scale by a power of two (the LSB moves). Pass **`--byte-align`** to emit the aligned
   field, or accept the exercised field as-is (the physical decode is identical when the
   flanking bits are constant). The snap never crosses another active field's bits, and
   never moves the MSB of a *signed* field (it would reinterpret the sign bit).

   **Sentinels / out-of-band outliers (agnostic detection; auto-drop the clear
   ones, report it).** A "signal invalid / unavailable" sentinel reads FAR outside
   the real band (a 2-byte speed at `0.1` → `0xFFFF` = 6553.5; engine-off RPM →
   `0x3FFF` = 16383). Detection is **agnostic** (`common.detect_extreme_outliers`):
   it does **not** assume the sentinel is all-ones, a single value, a small
   fraction, or on the high side. It rests on four scale-free pillars — a robust
   **median±MAD band**, a required **empty gap** separating the cluster from the
   bulk (so a legitimate full-range signal or a smooth ramp is *never* flagged), a
   per-frame **teleport** confirmation (a sentinel jumps in one frame; a real signal
   ramps), and **episode** counting with only a high sanity ceiling (no 2% frequency
   cap). All-ones/top/bottom codes are a *confidence hint only*. It returns a
   `confidence` (high/medium/low) and `kind` (sentinel/outlier/suspect). **Where it
   acts:** identification (correlate + bitsearch) **auto-masks high-confidence**
   detections before ranking (gated to ≤15% so a wrong field's scatter can't be
   masked away). **Deliverables:** the treatment is decided by confidence, not a
   blocking prompt — build_dbc/verify print `[!] EXTREME OUTLIERS …`, and:
   - **High confidence → drop without asking.** A high-confidence sentinel is an
     unambiguous "value unavailable" marker, so just pass `--drop-extreme` and
     **fold it into your final observations to the user** (which raw code, how many
     frames, what it decodes to) rather than interrupting the pipeline to ask.
   - **Medium / low confidence → ask the user** before dropping (it could be a
     genuine bistable state or a full-range excursion); then pass `--drop-extreme`
     only if they confirm.
   verify additionally **gates on the clean subset** at high confidence (so a
   correct field can't false-FAIL on kept sentinels) while still drawing them on the
   plot. Don't silently keep them — drop-and-report (high) or ask (med/low).

7. **Build a single-signal DBC from bitsearch's winner** → deliverables folder.
   Use the geometry bitsearch found on the sweep run, but **fit scale/offset on the HOLDS
   run** (clean anchors) with `--ref-window`:
   `python scripts/build_dbc.py --trace temp-output/trace_run_<holds>.csv \
       --sidecar temp-output/sidecar_run_<holds>.csv --ref-window 2 \
       --id 0x123 --order little --start-bit 8 --length-bits 16 --lag 0.2 \
       --name <signal> [--drop-extreme] --out decoding-output/<application>/<signal>/<signal>.dbc`
   (Byte-aligned: `--byte 1 --width 2 --order little|big`.) Two-stage robust fit
   (scale then offset). It **warns** if the derived scale is non-round (a tell of
   wrong geometry) and reports the residual distribution, not just R².
   **Auto-round (default on), gated on systematic bias — not R².** When the fitted
   scale lands near a neat OEM value, it computes the **worst-case systematic bias**
   that snapping would introduce vs the precise fit, as a fraction of range. Two
   tiers: (a) **bias ≤ ~1% → auto-applies** the clean scale/offset (a true
   noise-level cleanup, e.g. `0.0999 → 0.1`), printing `auto-round: … (bias <=X%)`;
   (b) **bias > ~1% → NOT applied** — it prints a `[!] round candidate NOT applied …
   would inject up to X% systematic bias` suggestion and keeps the precise fit.
   (R²/Spearman are nearly blind to a small scale error, so they can't gate this —
   that's why a snap like `0.0983 → 0.1` that reads ~3% high vs the reference is now
   *flagged, not applied*.) **When you see that flag, tell the user**: the precise
   fit matches the reference; the round line is only right if the reference is
   biased (e.g. dashboard/indicated speed vs OBD true speed) — let them choose, and
   force it with `--scale X --offset Y` if they want it. `--no-round` disables the
   whole step; `--drop-extreme` excludes sentinels from the fit and declared range.
   build_dbc also prints `systematic bias vs reference: …` for the final line.
   **Decimal tidy-up (default on, runs last on the final scale/offset).** The OEM
   snap above only fires for a value *near a neat OEM constant*; a genuinely
   non-OEM scale (a proprietary `0.0013822499…`) keeps its full fitted float tail,
   which is just fit noise — no encoder emits 18 significant digits. So a final
   step rounds **both scale and offset to the fewest decimal places** that does not
   move the decode by more than a tight precision budget (default **0.1 % of
   range**, `--round-decimals-tol`). It is **adaptive**: it tries 3 decimals, then
   4, 5, … and keeps the first that fits, so a coarse signal collapses to `0.001`
   while a fine one is allowed the extra digits it genuinely needs (`0.0013822499 →
   0.001382`, `0.00107095 → 0.001071`). It prints `decimals: scale … -> … (Ndp)
   (bias <=X% of range)`. This is orthogonal to the OEM snap (which handles
   `0.0999 → 0.1`); the tidy-up handles `0.0013822 → 0.001382`. `--no-decimal-round`
   keeps the full tail (the OEM snap still runs); `--no-round` disables BOTH the OEM
   snap and the tidy-up (fully raw fit). `calibrate.py` applies the same tidy-up.
   **Physical-anchor re-fit (default on), gated on the anchor disagreement.** Many
   signals must hit a *known* physical value at a *known* operating point — most
   universally **rest = 0** (speed, flow, current, power, torque read exactly 0 when
   idle). A free fit has no notion of this: against a quantized/laggy reference it
   spends a small offset minimising overall error, so the decode can read e.g.
   **−1.6 km/h while parked** — physically impossible, yet R²/Spearman are blind to a
   constant offset (just like they're blind to a scale error). build_dbc detects the
   reference's dense **rest cluster** and, when the decode there is off, **re-fits the
   line constrained to pass through the anchor** (re-deriving the slope from the data
   — *not* a mere offset shift, which would keep a slope fitted for the wrong offset
   and inject a motion bias). The gate mirrors auto-round: a **small** anchor
   disagreement (≤ ~6% of range) is a noise-level cleanup → auto-applied (`anchor:
   re-fit through the rest state …`); a **large** one is **flagged, not applied**
   (`[!] anchor NOT applied …`) because it means the field geometry is probably
   wrong — re-check bitsearch, don't paper a bad field over with an anchor. Only a
   **true-zero** rest is auto-detected; a non-zero anchor is signal-specific, so
   declare it with **`--anchor <value>`** (e.g. `--anchor 800` for an idle-RPM rest,
   which also forces the re-fit through that point). `--no-anchor` disables the step.
   *When you have a clean known value at a steady state, this is the strongest
   single constraint you have — lean on it.* (The honoured-anchor decode often shows
   a small residual `systematic bias` — that's the irreducible reference
   quantization/lag, now surfaced rather than hidden inside a bogus offset.)
   It emits a **fit diagnostic** (`4-fit-diagnostic.png`: raw-vs-reference scatter +
   fitted line, with a *rejected* round candidate drawn in red, plus a residual
   panel) into the signal's `analysis-plots/` (default from `--out`'s parent;
   override with `--plots-dir`) — the visual of the bias-gate decision.

8. **Verify — this is a GATE, not a formality.** Verify on the **HOLDS** run (absolute
   levels, with `--ref-window`); for extra confidence also verify the **SWEEP** run
   (tracking across the full range, no `--ref-window`).
   `python scripts/verify.py --trace temp-output/trace_run_<holds>.csv \
       --dbc decoding-output/<application>/<signal>/<signal>.dbc \
       --sidecar temp-output/sidecar_run_<holds>.csv --ref-window 2 \
       [--drop-extreme] --png decoding-output/<application>/<signal>/<signal>.png`
   Lag-aligns, scores **parked vs moving** segments separately, runs a
   reference-free self-consistency check, and prints **PASS** (exit 0) or
   **UNCONFIRMED** (exit 2) with a recommended action. **On UNCONFIRMED, do not
   accept the field** — go back to bitsearch (try the next candidate / another
   endianness), or ask for another excitation run (step 4), or reconsider the
   geometry. **Never explain a low score away as "just the reference quality."**
   The **plot overlays decoded (orange) and reference (blue) on ONE shared Y
   axis** (same physical unit) with a legend — any vertical gap is a real
   disagreement you can read at a glance, so always eyeball it. Show the PNG to the
   user. At **high confidence** verify **gates on the clean subset** automatically —
   it excludes detected sentinels from the verdict even without `--drop-extreme` (so
   a correct field can't false-FAIL on kept sentinels),
   while still drawing them on the plot; pass `--drop-extreme` to also remove them
   from the plot.
   verify also prints an **`abs. agreement`** line — mean signed bias + decoded-vs-
   reference slope (≈1 ideal) — computed **without** an affine refit, so a
   systematic scale/offset bias is surfaced (and `[!]`-flagged) even when Spearman
   is ~1. A flagged bias does NOT fail the gate (it can be legitimate, e.g.
   indicated speed) but you must report it to the user.

8b. **Calibrate scale/offset to known points (recommended for stationary signals).**
   The ramp fit nails the *field* but its absolute scale can drift (human lag).
   With known physical values, pin the line through them — and **validate against
   the moving data** so a few collinear holds can't hide a wrong field:

   **(i) Tagged anchors in one capture (the steady-holds app)** — park at each known
   value, hold, click it (logs `anchor` + dense `value`). Then:
   `python scripts/calibrate.py --id 0x9 --order little --start-bit 1 --length-bits 10 \
       --trace temp-output/trace_run_1.csv --sidecar temp-output/sidecar_run_1.csv \
       --window 2 --name <signal> --unit m/s^2 \
       --validate-trace temp-output/trace_run_1.csv --validate-sidecar temp-output/sidecar_run_1.csv \
       --out decoding-output/<application>/<signal>/<signal>.dbc`
   It reads the steady median raw per tag, fits the line, **warns on a non-round
   scale**, and — with `--validate-*` — flags `anchors fit but the moving data
   deviates` (the wrong-field trap).

   **(ii) Separate steady captures** — one short capture per point:
   `python scripts/calibrate.py --id 0x9 --order little --start-bit 1 --length-bits 10 \
       --point -9.81=temp-output/trace_xdown_1.csv --point 9.81=temp-output/trace_xup_1.csv \
       --name <signal> --unit m/s^2 --out decoding-output/<application>/<signal>/<signal>.dbc`
   2 points → exact line; 3+ → least-squares (R² flags nonlinearity). Re-run
   `verify.py` to confirm PASS. Same field flags as build_dbc, including the
   **auto-round** to neat OEM scale/offset (`--no-round` to disable).

9. **(Optional) Combine into the application-level DBC.**
   Use the **`combine-dbc`** skill — it scans
   `decoding-output/<application>/*/*.dbc` and merges every confirmed signal into
   `decoding-output/<application>/<application>.dbc`. Re-run any time you add a
   signal. (The low-level `merge_dbc.py` here merges one DBC at a time and still
   exists, but prefer `combine-dbc` for the structured, all-at-once combine.)

## Offline workflow — decode the reference from an existing log

Use this **instead of** the live capture above when the user **already has a
recorded CSV log** containing both the proprietary raw CAN data and a **separately
decodable reference** — any source they have a DBC for: OBD2 responses, a
GPS‑to‑CAN / CANmod / sensor‑to‑CAN module. The reference is machine‑decoded
(near‑zero lag), so it usually fits **far better** than digitized human holds — it's
just coarser/sparser. (Use this only when such a reference exists; otherwise capture
a live human reference with the steady-holds app.) **Skip `detect_bus`, `capture`,
`flask_sync`/`annotate`
entirely.** Reference‑source‑agnostic: we decode strictly what the user's DBC
defines; multiplexed DBCs (e.g. OBD2) decode transparently via cantools.

**Bundled OBD2 DBC — use it for any OBD2 reference.** This skill ships the standard
OBD2 PID database at **`assets/obd2-dbc/OBD-v4.4.dbc`** (relative to this skill's
directory). Whenever the reference is an **OBD2 response** — engine RPM, vehicle
speed, coolant temperature, throttle, etc. (typically on response IDs `0x7E8`–
`0x7EF`) — pass this file as `--dbc`; do **not** ask the user for an OBD2 DBC.
cantools decodes its multiplexed PID messages directly, so `--signal rpm` /
`--signal speed` resolve the right PID. (For a GPS/GNSS reference, use the bundled
GPS DBCs below; only ask the user for a DBC for some *other* non-bundled source —
e.g. a third-party sensor-to-CAN module.)

**Bundled GPS/GNSS DBCs — use these for a CSS Electronics GPS reference.** Many
CANedge recordings include a **GPS/IMU reference on the same SD card** as the
proprietary vehicle data — either from a **CANedge with internal GPS/IMU** (the
GNSS variants) or from a **CANmod.gps** GPS-to-CAN module wired onto one of the
CANedge/CANsub CAN channels. GPS **speed** (`GnssSpeed.Speed`, m/s, machine-decoded
and near-zero lag) is an excellent reference for reverse-engineering a proprietary
**vehicle-speed** signal; the IMU **acceleration / angular-rate** channels make good
references for proprietary accel/gyro signals, and position/heading/altitude are
available too. This skill ships both databases (relative to this skill's directory):

- **CANmod.gps** (GPS-to-CAN module) → **`assets/gps-dbc/canmod-gnss.dbc`**. GNSS/IMU
  messages on CAN IDs `0x1`–`0x9` (`GnssStatus`, `GnssTime`, `GnssPosition`,
  `GnssAltitude`, `GnssAttitude`, `GnssOdo`, `GnssSpeed`, `GnssGeofence`, `GnssImu`).
- **CANedge internal GPS/IMU** (recorded on the device's internal **CAN9** channel) →
  **`assets/gps-dbc/canedge-internal-can9.dbc`** (firmware 01.09). Same GNSS/IMU set
  on IDs `0x65`–`0x6F`, plus device housekeeping (`Heartbeat`, `TimeCalendar`,
  `TimeExternal`, `ImuAlign`).

Pass the matching file as `--dbc`; do **not** ask the user for a GPS DBC. Pick the
file by source: **internal CANedge GPS → `canedge-internal-can9.dbc`** (the GPS data
sits on the internal CAN9 message IDs), **CANmod.gps module →
`canmod-gnss.dbc`**. If unsure which, run `decode_reference.py` (step 1) without
`--signal` against each and use the one whose IDs are present in the log.
`--signal speed` resolves `GnssSpeed.Speed` (a case-insensitive *exact* match wins
over the `SpeedAccuracy` substring, so it's unambiguous); use `--signal AccelerationX`
etc. for the IMU channels. As always, note the printed **`--exclude-ids`** (the GPS
message IDs) and pass it to
every search step so the search can't self-match inside the GPS frames. **Note on
CAN IDs:** the GPS IDs are configurable on the device, so if the log's GPS frames sit
on different IDs than the DBC defaults, the user can edit the `BO_` IDs (or you can
confirm the real IDs from a `survey.py` / `decode_reference.py` listing).

The log must be **webCAN CSV** (the `python-can-cansub` native format, same header
the live capture writes). A **CANedge** user can produce it from an MF4 log with
the **mdf2csv** converter (see the `process-log-files` skill) and then use this
workflow unchanged.

0. **Locate the log (don't ask — glob).** If the user named the file (e.g.
   `chevy-tahoe-obd2-can-data.csv`) or said "my log/CSV", find `<log.csv>` by
   globbing the **working-directory root first**, then **`TEMP/` recursively**
   (`**/<name>`). Use the match as `<log.csv>` below; only ask if nothing or
   several plausible files match. Also establish `<application>` / `<signal>` names
   from the request (here e.g. `chevy-tahoe` / `vehicle-speed`).

1. **Decode + confirm the reference.** First list what's decodable (covers "user
   forgot which signal / which DBC"). For an OBD2 reference, `<reference.dbc>` is
   the bundled `assets/obd2-dbc/OBD-v4.4.dbc`; for a CSS Electronics GPS reference it
   is `assets/gps-dbc/canmod-gnss.dbc` (CANmod.gps) or
   `assets/gps-dbc/canedge-internal-can9.dbc` (CANedge internal GPS on CAN9) — see the
   bundled-DBC notes above:
   `python scripts/decode_reference.py --trace <log.csv> --dbc assets/obd2-dbc/OBD-v4.4.dbc`
   Then decode the chosen signal into a sidecar + plot:
   `python scripts/decode_reference.py --trace <log.csv> --dbc <reference.dbc> \
       --signal speed --label speed_ref --out temp-output/sidecar_speed_ref.csv \
       --png decoding-output/<application>/<signal>/analysis-plots/0-decode-reference.png`
   (the `0-` prefix sorts the reference-confirmation plot ahead of the numbered
   raw-bus analysis plots `1-`…`4-` in the signal's `analysis-plots/` folder.)
   `--signal` is a case‑insensitive substring of the DBC signal name (so `speed`
   resolves whatever the DBC calls it; if several match, the data signal with far
   more samples is picked and reported, else it stops as ambiguous). **Show the PNG
   to the user to confirm the reference looks right** before searching. Note the
   printed **`--exclude-ids`** (the reference's source IDs) — pass it to every
   search step so the search can't trivially self‑match inside the reference frame.

2. **Survey the raw bus**, excluding the reference source:
   `python scripts/survey.py --trace <log.csv> --exclude-ids <src-ids> \
       --plots-dir decoding-output/<application>/<signal>/analysis-plots/`

3. **Correlate** raw vs the decoded reference, excluding the source:
   `python scripts/correlate.py --trace <log.csv> \
       --sidecar temp-output/sidecar_speed_ref.csv --type continuous \
       --exclude-ids <src-ids> --plots-dir …/analysis-plots/`

4. **bitsearch → build_dbc → verify** exactly as in steps 6–8 above, against
   `<log.csv>` and the decoded sidecar (pass `--plots-dir …/analysis-plots/` so the
   R² grid and fit diagnostic land beside the others). The verify **gate** works the
   same (idle vs driving = parked vs moving). Expect a strong PASS.
   **Honest‑failure path:** if nothing correlates or verify is UNCONFIRMED, the raw
   bus may simply not encode that signal — report that rather than forcing a fit.

The decoded sidecar is a normal `kind=value` reference, so the whole downstream
pipeline is unchanged. `calibrate.py` is rarely needed here — the decoded
reference is already in physical units.

## Vision workflow — digitize a reference from a video of a display

Use this when the user has **no decodable on-bus reference** (rules out the Offline
workflow) and **cannot run a live capture** (rules out the Flask app), but **can
record a video of a display** (dashboard, gauge app, instrument cluster) that shows
the true physical value while the CAN data is logged. A local, open-source OCR script
(`vision_reference.py`) reads the on-screen number per frame into a standard
`kind=value` sidecar; the rest of the pipeline runs **exactly like the Offline
workflow** — with one difference: the reference is **off-bus**, so there is **NO
`--exclude-ids`** (nothing to self-match against).

**Inputs the user provides (two files):** a **webCAN CSV** of the proprietary CAN data
(same format the Offline workflow expects) and a **video** of the display. The user is
responsible for the rough time-sync — the standard setup is webCAN streaming the CSV
**while** a camera/webcam records the display in parallel.

**Scope (v1):** **numeric / digital readouts only** (a number like `72.5`, `48 km/h`,
`91 °C`, `100 %`). Analog needle/dial gauges are out of scope. OCR is local
(RapidOCR / ONNX, no PyTorch) — see the deps note below.

**Time base — why this aligns, and the anchor that actually works.** The video's
capture-start time + each frame's delta give an **absolute epoch per frame**; the webCAN
CSV is also absolute, so the two align **directly**. `vision_reference.py` anchors frame 0
to **`com.apple.quicktime.creationdate`** (the iPhone CAPTURE-START time, timezone-aware)
— **NOT** the mvhd **`creation_time`**, which is often the FILE-FINALIZE/save time and was
measured **+9.5 / +37 / +15 s** after capture start on three real iPhone clips (variable,
not a fixed offset — do not trust it). With the capture-start anchor the residual is just
the device-clock sync error plus the 1-second quantization of `creationdate`, **~1–2 s**,
absorbed by the **lag search**. So use a **wide** lag window for the video case
(`correlate --max-lag 2`, the default; `bitsearch --max-lag 2`; `verify --max-lag 2`) vs
the tight `~0.2 s` you'd use for a clean machine reference.

**When the anchor is missing or wrong (non-iPhone, re-wrapped file, or the decode won't
align):** there is no `creationdate`, so the script falls back to `creation_time` and
**warns**. Don't trust that blindly — have the user record a **short clock-test clip of
the webCAN header clock** (the HH:MM:SS it shows) for a few seconds in the SAME session,
then measure the offset and apply it:
  `python scripts/vision_reference.py --video <clock-test.mov> --measure-clock <x,y,w,h>`
  (find the clock ROI with `--dump-frames`; add `--display-utc-offset <hours>` if the clock's
  local timezone can't be inferred from the anchor). It prints the **anchor-vs-true-clock
  offset** and the exact **`--time-offset <s>`** to pass when extracting the real clip
  (same session ⇒ same offset). If recordings started far apart, you can also set
  `--start-epoch <epoch>` directly. The device clock is **not** guaranteed NTP-accurate
  (the CANsub seeds its clock from the host PC on connect), so this clock-test is the
  reliable way to pin the offset when the metadata can't.

0. **Locate both files (don't ask — glob).** Find the named CSV and the video by
   globbing the **working-directory root first**, then **`TEMP/` recursively**. Establish
   `<application>` / `<signal>` and the displayed **quantity + unit** from the request
   (confirmed against the frames in step 1).

1. **Inspect frames → set the ROI + unit.** Dump a handful of frames spread across the
   clip and **look at them yourself**:
   `python scripts/vision_reference.py --video <video> --dump-frames 9`
   This writes `temp-output/vision-frames/fNN_t<sec>.png` and prints the duration +
   `creation_time`. **Read the frames** to (a) read the on-screen **quantity and unit**
   and (b) pick the **digit region** `x,y,w,h`. Choose an ROI generous enough to enclose
   the digits at **every** value in the clip (the digit count changes, e.g. `9.7` →
   `100.0`). Do **not** ask the user for the location — determine it from the frames.

2. **Extract the reference.** OCR the chosen ROI across the clip into a sidecar:
   `python scripts/vision_reference.py --video <video> --roi <x,y,w,h> \
       --label <signal>_ref --unit <unit> --fps 20 \
       --png decoding-output/<application>/<signal>/analysis-plots/0-vision-reference.png`
   (`--fps` floor is 1 Hz; 20 Hz is typical. `--conf` drops low-confidence reads;
   `--max-jump` rejects implausible OCR jumps; `--min/--max` clamp.) It writes
   `temp-output/sidecar_<signal>_ref.csv` (the standard `epoch;kind;label;value`), a
   `*_diagnostics.csv` (raw vs cleaned + confidence), a `*.meta.json` (consumed by the
   review app), the `0-vision-reference.png` confirmation plot, and prints the **OCR read
   rate + mean confidence**, the **auto-clean line** (how many outliers it rejected), and
   the `survey`/`correlate` next-commands (**no `--exclude-ids`**).
   **Outliers are the norm, and cleaning is automatic + free to re-tune — do NOT plan a
   second OCR pass.** Real display footage almost always has OCR glitches (glare spikes,
   a digit lost to motion-blur reading 0 mid-drive, a glance-away), so `vision_reference.py`
   runs an **adaptive jump-outlier reject ON BY DEFAULT** — a unit-agnostic running-median
   filter whose threshold is derived from the data's own robust range (no need to guess a
   per-signal `--max-jump` up front). It reports what it dropped. If you still need to
   re-tune the cleaning (tighter `--conf`, a `--min/--max` clamp, a manual `--max-jump`),
   use **`--reclean`**: it re-derives the sidecar from the cached `*_diagnostics.csv`
   **without re-running OCR** (seconds, not minutes — the raw per-frame reads are already
   cached). `--no-auto-clean` keeps the raw reads (only explicit `--conf`/clamps/`--max-jump`
   apply). So the normal flow is **one OCR pass**; reach for `--reclean` only to adjust.

3. **Confirm the reference against the video — MANDATORY GATE (OCR can misread).** Launch
   the review app and have the **user** confirm it tracks the on-screen number:
   `python scripts/vision_review.py --video <video> --sidecar temp-output/sidecar_<signal>_ref.csv`
   Open `http://127.0.0.1:5001` — video (top-left), a big **stat readout** of the current
   digitized value (top-right), and the **uPlot reference with a playhead** (bottom);
   Play/Reset + native scrub keep them in sync. Also show `0-vision-reference.png` and the
   read-rate/confidence. **ALWAYS launch `vision_review.py` and have the user confirm —
   even when `0-vision-reference.png` and the OCR metrics look excellent.** A high mean
   confidence and a clean-looking plot still hide per-frame glitches (glare spikes, a digit
   lost to motion-blur reading 0 mid-drive, a glance-away); the PNG and metrics are a
   PREVIEW, never a substitute for the app. Do NOT skip the app and proceed to `correlate`
   on the strength of the metrics alone. **Do not proceed until the user confirms** the
   digitized value tracks the on-screen number through the clip, especially across
   transitions. If OCR is
   poor: a **tighter ROI** needs a fresh OCR pass (re-run step 2), but any **cleaning**
   change (higher `--conf`, a manual `--max-jump`, a `--min/--max` clamp) should go
   through **`--reclean`** — it re-cleans the cached reads in seconds instead of
   re-OCRing. The default auto-clean usually makes one pass enough.

4. **survey → correlate → bitsearch → build_dbc → verify** exactly as in the Offline
   workflow steps 2–4 / the live steps 6–8, against `<log.csv>` and the vision sidecar —
   but **omit `--exclude-ids`** (the reference is off-bus) and pass the **wide lag** to all
   three search steps: `correlate --max-lag 2`, `bitsearch --max-lag 2`, `verify --max-lag 2`
   (bitsearch accepts `--max-lag` as the symmetric-window equivalent of its
   `--lag 0 --lag-refine 2`). Set `--name <signal>` / `--unit` from
   the display. The verify **gate** is unchanged; eyeball the overlay. **Honest-failure
   path:** if nothing correlates or verify is UNCONFIRMED, the raw bus may not encode that
   signal, or the OCR reference may be too laggy/noisy — report it rather than forcing a
   fit (and re-check the step-3 review first).
   **Indicated vs true (esp. dashboard SPEED): a few-% non-round scale is EXPECTED, not a
   defect.** A displayed speedometer reads *indicated* speed, which by regulation never
   under-reads true ground speed — so it runs a few % high. Fit against it and the scale
   lands a few % ABOVE the clean OEM value (e.g. a wheel-speed field's true `1/64` reads
   as ~`0.0167`), tripping build_dbc's `[!] non-round scale` warning. Here that warning is
   benign — the precise fit matches the *display* (which is what the user filmed); the
   round value would be the *true* wheel/ground speed. Report both, don't force the round
   scale. (A car also carries speed on SEVERAL IDs — per-wheel speeds, a cluster value, an
   OBD2 echo on `0x7E8` — so correlate legitimately returns a tie-group of real carriers;
   confirm each with bitsearch and pick by fit + update rate. This is a genuine multi-
   carrier tie, NOT the sparse-reference degeneracy the live-holds note warns about.)

The vision sidecar is a normal `kind=value` reference, so the whole downstream pipeline
is unchanged.

## Notes for the assistant

- **Trust the tools over hand-rolled diagnostics.** `bitsearch` (identification)
  and `verify` (the gate) already encode the robust logic — a reference-free
  plausibility prior (wrap-free / smooth decoded series), R²-led ranking, and
  park-vs-move scoring. Don't override their conclusions with an ad-hoc check
  (e.g. eyeballing "smoothness"); a wrong field can look locally smooth.
- **Don't theorize from the calm baseline scan; the DELTA + correlation decide (the
  "looks like N signals" trap).** On the first, calm bus scan almost everything is
  static — the parked target included — so a byte pattern there is NOT evidence either
  way: a tidy `8×0x80` is no more "= 8 signals" than it is "= definitely not signals."
  Forming a hypothesis (positive OR negative) from that single scan is premature. What
  is informative is the **delta** between the calm baseline and an exercised/sweep scan
  (`survey --baseline`) plus `correlate` / `bitsearch` scored against the reference —
  decide the carrier from those, never from what a frame "looks like." (A fast frame
  whose low byte merely *dithers* is a candidate analog signal, not automatically a
  counter; and `correlate`'s #1 is a result to confirm with bitsearch, not to discard
  because another frame's pattern fits a story.) This is the exact failure that
  motivated the sweep workflow: the true field was `correlate`'s #1, overridden by an
  "`8×0x80` = 8 gauges" story that was really eight zeroed pulse counters — and it was
  the **baseline-vs-sweep delta**, not the calm scan, that actually disambiguated.
- **Show the analysis plots; use them to widen the search.** survey / correlate /
  bitsearch / build_dbc each auto-emit a polished PNG into the signal's
  `analysis-plots/` (pass `--plots-dir …/analysis-plots/`; `--no-plots` to skip).
  Surface them to the user. They are decision-explainers and blog-ready, but also
  working tools: the bus heatmap and correlation heatmap often reveal *other*
  candidate IDs/bytes (and related signals) worth decoding next — don't tunnel on
  the single winner.
- **How each encoding choice is decided (permutation transparency).** `bitsearch`
  enumerates *all* of {start-bit × length × little/big × signed/unsigned} per ID,
  scores each, and prints a **Decision** line: the winning order, signedness, the
  fitted **scale including its sign** (negative = anti-correlated reference), R²,
  plausibility and lag. The scale's sign comes from the fit; you don't pick it.
- **correlate's shortlist ranks on (scale-free) Spearman, then breaks ties on the
  scale-AWARE linear-fit R² (the `r2` column), then plausibility.** Spearman alone
  rates a coarse or wrong-scale slice that merely *moves with* the signal at ~1.0, so
  several IDs routinely tie at the top; the `r2` column is what separates a genuine
  carrier (R²≈1.0) from a worse-fitting one (e.g. a speed slice at R²≈0.93). When you
  see a tie-group, **compare the `r2` column and confirm the top-R² carriers with
  bitsearch** — don't read correlate's #1 as the answer. (A car carries speed on
  several real IDs — per-wheel + cluster + an OBD2 echo — so a multi-carrier tie of
  genuine carriers is normal; pick among them by fit + resolution + update rate.)
- **Read the survey ID count from its final `== N unique IDs ==` line or the survey
  JSON, never from a truncated stdout tail.** survey output is long (a per-ID table +
  a bit-packed field-map list); piping it through `tail` drops the authoritative count
  and leaves only the field-map subset, which is NOT every ID. The same applies
  whenever you summarise a long tool output — confirm totals against the machine-
  readable artifact (the JSON), not a scrolled-off console tail.
- **A non-round scale or an UNCONFIRMED verify means the field is probably wrong**,
  not that the reference was bad. Loop back to bitsearch / capture another run.
  (Caveat: *round* doesn't prove *right* — an over-wide read of a `2^−j`-scaled
  field keeps a binary-fraction scale. bitsearch's parsimony rule, not the scale
  check, is what demotes that; see step 6 and `references/signal-encoding.md`.)
- **Sentinels/outliers → detection is agnostic; treatment is gated by confidence,
  not a blocking prompt.** The detector (`common.detect_extreme_outliers`) does NOT
  key on the sentinel value, bit pattern, fraction, or sign — it uses a robust
  median±MAD band, a required empty gap, a per-frame teleport check, and episode
  counting (no 2% cap; all-ones/top/bottom is only a *confidence* hint).
  **Identification auto-masks** high-confidence detections before ranking (correlate
  + bitsearch, gated ≤15%), and **verify gates on the clean subset** at high
  confidence. For a **deliverable** (build_dbc / final verify): a **high-confidence**
  hit is an unambiguous "value unavailable" marker, so **drop it without asking**
  (pass `--drop-extreme`) and **report the decision in your final observations** —
  don't block the pipeline on a question whose answer is obvious. Only when the
  detection is **medium/low confidence** (it could be a genuine bistable state or a
  full-range excursion) do you **ask the user** before dropping. Never silently keep
  a high-confidence sentinel either — drop-and-report. (If a real signal is genuinely
  *bistable*, a far minority state can look sentinel-like — it never reaches "high"
  for a wide/balanced split, so identification won't mask it; that's exactly the
  med/low case you surface to the user.)
- **Auto-round is bias-gated, and "round" must not mean "biased."** build_dbc /
  calibrate snap scale/offset to neat OEM values only when the snap shifts the
  decode by ≤~1% of range (a noise-level cleanup); a snap that would inject a
  larger *systematic* bias is **flagged, not applied**, and the precise fit is
  kept. Never override that by hand-forcing the round value unless the user agrees
  the reference itself is biased (e.g. dashboard/indicated speed vs OBD true
  speed). Don't trust R²/Spearman to vet a scale — they're blind to a few-percent
  multiplicative bias; read the **`abs. agreement`** line in verify (mean bias +
  slope) and the `systematic bias` line in build_dbc instead. A clean decode of a
  *true* signal should show ~0 bias and slope ≈ 1.
- **Decimal tidy-up keeps DBCs readable without losing precision.** Separate from
  the OEM snap: build_dbc / calibrate also round scale **and** offset to the fewest
  decimal places that stays within ~0.1 % of range (`--round-decimals-tol`),
  adaptively (3 dp, then 4, 5, …). This drops the meaningless float tail on a
  *non-OEM* scale (`0.0013822499 → 0.001382`) that the OEM snap deliberately leaves
  alone. It is a near-zero-bias cleanup, on by default; it prints a `decimals: …`
  line with the bias it introduced. Disable with `--no-decimal-round` (keep the
  tail, OEM snap still runs) or `--no-round` (fully raw fit). If a signal genuinely
  needs many decimals to stay precise, the tidy-up keeps them — it never trades
  precision for a shorter number.
- **Use physical anchors — a decode that's wrong at a known state is wrong.** If a
  signal must equal a known value at a known operating point (the universal one:
  **rest = 0** for speed/flow/current/power/torque), the decode MUST hit it.
  R²/Spearman are blind to a constant offset, so a fit can score ~1.0 yet read
  −1.6 km/h while parked — illogical. build_dbc's **physical-anchor re-fit** (on by
  default) detects the reference's rest cluster and re-fits the line *through* that
  anchor; treat "non-zero decode at a clearly-zero state" as a real defect to fix,
  not a rounding nicety. The gate is the anchor *disagreement*: a small one is an
  honest cleanup (auto-applied); a large one is **flagged, not applied** — a big
  miss at a certain point means the field geometry is wrong (loop back to
  bitsearch), never paper it over with an offset. Only true-zero is auto-detected;
  for any other known steady value pass **`--anchor <value>`**. Anchors aren't
  always available, but when you have a clean one, it is the single strongest
  constraint on the calibration — lean on it.
- Every script supports `--help`. Pass `--device <id-substring>` to disambiguate
  if multiple CANsubs are connected; otherwise the first discovered is used.
- For discrete-STATE targets (lock, gear, wiper), use `--mode holds` with the state
  values and the normal `--type continuous` pipeline — a few-level hold finds the
  bit/field. Only a *truly momentary* event you cannot hold needs the headless
  `annotate.py --mode event` + `correlate --type discrete` fallback (no flask tab).
- `python scripts/common.py --selftest` validates bitfield extraction ↔ cantools
  mapping AND the analytical primitives (plausibility, scale-roundness,
  signed-Spearman, **agnostic sentinel detection** — fraction>2%, multi-value /
  non-all-ones / signed most-negative sentinels detected; smooth ramps & full-range
  continuous signals NOT flagged — auto-round, **decimal tidy-up** (drops a long
  float tail to the fewest decimals within the precision budget, keeps the digits a
  fine signal needs, no-op on an already-short value), **physical-anchor re-fit**
  (true-zero auto / already-honoured / non-zero-skip / forced / large-disagreement-flag),
  theoretical-range, **resolution refinement** (a real ramp grows the field LSB-ward,
  a noise neighbour stops it, the **flip-rate cascade** recovers width from no-ramp/
  dithering data, a rolling counter below the field is NOT swallowed) and **byte-align
  snap** (extends across constant bits to a standard width, blocked by an active
  neighbour, never moves a signed MSB)) AND headlessly smoke-tests all the analysis
  plots (incl. the `3c-bit-cascade.png`); run it after editing identification/extraction, the outlier detector, the
  anchor/round or resolution calibration, or plotting logic.
  `python scripts/bitsearch.py --selftest` validates the overlap/parsimony ranking
  (over-wide read demotion, high-byte/wrapping-slice rejection), **that auto-masking a
  sentinel flips the winner from a coarse slice back to the true wide field**, and
  **that the flip-rate cascade guard keeps a wide field whose extra LSBs continue the
  cascade while still demoting a genuine over-wide read**; run it after editing
  `_rank_key` / `_suppress_overlaps` / the masking / the cascade guard.
- `flask_sync.py` is the "CANsub Reference Generator" with a **Holds | Sweep** toggle
  (`--mode both`, default; or `--mode holds` / `--mode sweep` to force one) and serves a
  vendored uPlot from `assets/` (offline, no network). **HOLDS** = settle + click each
  known value → `kind=anchor` windows (analyse with `--ref-window`); **SWEEP** = drag a
  vertical slider → dense `kind=value` at ~20 Hz, no anchors (analyse WITHOUT
  `--ref-window`). Launch it **fully configured** (`--unit`, `--min/--max`,
  `--state-values`, `--window`, optional `--instructions`). Each Start writes a fresh
  numbered `trace_run_<N>.csv` + `sidecar_run_<N>.csv` pair **tagged holds/sweep**
  (reuses `capture.resolve_config` + `bus.json`); query `/api/runs` or read
  `runs_<label>.json` to see runs and their mode. `--port` if 5000 is taken,
  `--listen-only` for fully-passive multi-node sniffing. (Headless twins:
  `annotate.py --mode continuous` ≈ sweep, `annotate.py --mode event` = momentary.)
- **Vision reference (`vision_reference.py` + `vision_review.py`).** For the **Vision
  workflow**, `vision_reference.py` has these modes: `--dump-frames N` writes evenly-spaced
  frames for **you** to inspect (set the ROI + read the unit — never ask the user for the
  digit location), then `--roi x,y,w,h` OCRs the clip into a standard `kind=value` sidecar
  (+ diagnostics, a `.meta.json`, and the `0-vision-reference.png`). It anchors epochs to
  the iPhone **`com.apple.quicktime.creationdate`** (capture start), falling back to
  `creation_time` with a warning (that field is the unreliable file-finalize time — see the
  **Time base** note); analyse with a **wide lag** (`correlate/bitsearch/verify --max-lag 2`)
  and **without `--exclude-ids`** (off-bus reference). When no reliable anchor exists, the
  **`--measure-clock x,y,w,h`** mode OCRs a short webCAN clock-test clip and prints the
  **`--time-offset`** to apply (see the **Time base** note). **Cleaning is automatic and
  re-tunable without re-OCR:** an **adaptive jump-outlier reject is ON by default**
  (unit-agnostic running-median filter, threshold from the data's robust range — no
  per-signal `--max-jump` needed; `--no-auto-clean` to disable), and **`--reclean
  [diag.csv]`** re-derives the sidecar from the cached `*_diagnostics.csv` in seconds, so a
  cleaning tweak (`--conf`/`--min`/`--max`/`--max-jump`) never costs a second OCR pass —
  only an **ROI** change does. **ALWAYS run `vision_review.py` and have the user confirm
  the digitized reference tracks the video before searching — even when the
  `0-vision-reference.png` and OCR metrics look excellent** (high mean confidence hides
  per-frame glitches). The PNG/metrics are a preview, not a substitute; that interactive
  visual check is the gate. Numeric/digital readouts only (no analog needle gauges). Needs
  `rapidocr` + `onnxruntime` + `opencv-python` (in `requirements.txt`).
- Reference material: `references/re-methodology.md` (the manual method this
  automates, plus the gate / multi-run discipline) and
  `references/signal-encoding.md` (bit order, sign, scale/offset, bit-packing,
  the plausibility prior, DBC start-bit conventions).
