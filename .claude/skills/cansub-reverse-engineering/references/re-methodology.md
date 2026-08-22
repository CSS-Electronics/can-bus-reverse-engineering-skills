# CAN signal reverse-engineering methodology

Condensed from the CSS Electronics guide *"CAN Bus Sniffer — Reverse Engineering"*.
This is the manual workflow the skill automates; use it to choose which script to
run and how to interpret results.

## Signal taxonomy (decide this first)

- **Stationary vs driving**
  - *Stationary*: reproducible while parked (door lock/unlock, wipers, AC, windows).
    You can trigger the action on demand → use **discrete event** annotation.
  - *Driving*: only varies in real operation (speed, RPM, SoC, GPS). Needs a trip
    and a reference value logged over time → use **continuous value** annotation.
- **Discrete vs continuous**
  - *Discrete*: a small set of states (lock=0/1, gear=P/R/N/D). Usually **1–2 bits**.
    scale=1, offset=0 typically suffice.
  - *Continuous*: varies smoothly over a range. Usually **1–2 bytes**, often on a
    clean byte border. Needs scale + offset.

## Safety & profile: live vehicle vs bench (decide at init)

`detect_bus.py --profile` sets how the CANsub behaves on the bus for the whole
session:

- **vehicle (default, safe)** → SILENT / `listen_only`. For any existing active
  multi-node bus (car, truck, bike, machine). Other ECUs provide the ACK; the
  CANsub transmits nothing. **Use this whenever unsure.**
- **bench** → NORMAL / ACK. Only for a *single* node on a desk (one ECU/sensor):
  with nobody else to ACK, the CANsub must ACK or the lone node error-floods.

On a live vehicle: prefer silent; this skill never transmits **data** frames
(no replay/injection) — confirming a discrete signal by replaying a frame is
deliberately out of scope. Never reverse-engineer on a *moving* vehicle. Bit-rate
probing is always passive. The init step reads the device bus status
(state / frame-rate / RX-TX-bus error counters) and warns if the bus is unhealthy
(bus-off, error-passive, idle, or erroring) — a fast way to catch a wrong profile,
wrong bit-rate, wiring/termination fault, or a dead bus before capturing.

## The 8 steps (and how the skill maps to them)

1. **Decide target signal & type.** — pick taxonomy above.
2. **Select adapter cable.** — OBD2-DB9 (cars), J1939-DB9 (trucks). Manual.
3. **Determine bit-rate & connect.** — `detect_bus.py` probes passively.
4. **Compare real-world vs CAN data** ("needle in a haystack"). — `capture.py`
   + `annotate.py` to record a synchronized reference; `survey.py` to shrink the
   candidate set (which bytes even move).
5. **Identify bit position & length.** — `correlate.py` ranks candidate
   (ID, byte/bitfield, endianness); `bitsearch.py` (phase 2) refines exhaustively.
6. **Identify scale & offset.** — `build_dbc.py`: derive **scale first** (match the
   standardized shapes / robust slope), **then offset**. Iterate, do not solve both
   jointly.
7. **Add entry to a DBC.** — `build_dbc.py` writes a single-signal DBC.
8. **Transmit for requests/control.** — out of scope for this passive skill
   (the CANsub *can* transmit, but this skill stays read-only by design).

## Why this is hard manually (what we automate away)

- Step 4 is the bottleneck: hundreds of IDs × 64 bits, frames streaming by — a
  human watching webCAN/SavvyCAN fade-mode is slow and error-prone.
- Continuous scale/offset by eye = iterative curve matching against sparse,
  noisy reference data.
- Human reference input is **imperfect**: reaction lag, occasional wrong inputs,
  gaps. Correlation must tolerate this (lag search, rank-based + outlier-robust
  scoring) — see `correlate.py`.

## Verification is a gate, not a formality (the failure this prevents)

A candidate can correlate with the reference and even decode correctly at a few
calibration points while being the **wrong encoding** — a scrambled but
deterministic function of the true field. The classic tell: it matches when the
signal is **parked** (held at a value) but **diverges in motion**. `verify.py`
scores parked vs moving segments separately and runs a reference-free
self-consistency check (the decoded series must be smooth and wrap-free), then
returns **PASS / UNCONFIRMED**. Treat UNCONFIRMED as a stop: re-run `bitsearch`,
capture another excitation run, or reconsider the geometry. **Do not** rationalise
a low score as "the human reference was noisy" — for the correct field, parked
AND moving both agree.

## Two excitations: a SWEEP to identify, HOLDS to calibrate

The live reference has two complementary jobs, and a single excitation does neither
well alone — so for a continuous signal capture BOTH (the app's **Holds | Sweep**
toggle records each as a tagged run):

- **SWEEP (continuous slider) → identification + resolution.** Drag the slider to track
  the signal smoothly across its whole range (`flask_sync --mode sweep`, dense
  `kind=value`, analysed WITHOUT `--ref-window`). Continuous variation makes the ranking
  *discriminating*: on a smooth ramp the true field scores ~1.0 while counters and
  unrelated traffic fall to ~0, whereas a few **discrete holds** let counters spuriously
  rank-match the monotone step pattern (correlate goes degenerate — many candidates tie at
  ~1.0, which is how a wrong field can be chased). The transitions also drive bitsearch's
  **resolution-refinement**, recovering the field's FULL width; discrete holds reveal only
  the MSB and under-read it as a narrow high-bit slice.
- **HOLDS (steady anchors) → calibration + absolute verify.** Settle at known levels and
  click each (`--mode holds`, `kind=anchor`, analysed WITH `--ref-window`): low-noise held
  data is the clean anchor for scale/offset and absolute-level checks.

Identify on the sweep, calibrate on the holds. When one run is ambiguous, capture
**several sweeps with different excitation** (slow vs vigorous, different
axes/conditions) and require the same field to win across them — a field that only wins
under one specific motion is suspect. (Discrete-state signals — lock/gear/wiper — need
only a holds run; there is nothing to sweep.)

## Calibrate against known physics, validate against motion

Pin scale/offset through known points (e.g. an accelerometer's ±1 g gravity holds)
rather than a hand ramp — but remember **2–3 collinear points fit any line with
R²≈1**. Always validate the fitted line against the *moving* data
(`calibrate.py --validate-trace/--validate-sidecar`): if the holds fit but the
motion deviates, the field geometry is wrong, not the calibration.

## Separating collinear signals — split by the divergence regime

Engine-demand channels (throttle / pedal / MAF / load / MAP / torque) rise together,
and warming temperatures climb together, so each one correlates with the *others'*
bytes and `correlate` will happily rank a proxy first. A moderate score (R²≈0.5–0.95)
landing on a byte already assigned to a physically related signal is the warning. Two
discriminators cut through it. First, the field's own **absolute value** at a
distinctive operating point (the warm-end temperature, 0 at rest, atmospheric at WOT) —
the true field must read *that* value, a neighbour's will not. Second, and sharper when
the log also carries continuous references for the co-variates, the **divergence
regime**: restrict the correlation to the operating window where the target and its
co-variates physically pull apart. Engine overrun (foot off, revs up)
drives torque negative while airflow stays low-positive, so a real torque field tracks
torque down there while a load proxy keeps following air. Choose a log that actually
*exercises* that divergence — a cruise-only capture never separates the cluster. A high
Spearman with a poor R² (rank-monotonic but not linear) points to a proxy rather than the
value — but only *after* the fixable culprits (unsolved lag, wrong endianness/signedness,
saturation/sentinel clipping, a reference-semantics mismatch like %-of-reference vs
absolute) are ruled out, since each produces the same pattern. Then confirm by the regime
split — the concrete build→mask→filter→re-correlate recipe lives in SKILL.md's
Divergence-regime proxy test section — not by chasing a tighter global fit.

## A decoded log reference beats the human ramp

When the user already recorded a log that *also* carries a decodable reference
(OBD2, GPS-to-CAN, CANmod, any sensor-to-CAN with a DBC), decode that reference
straight from the log (`decode_reference.py`) instead of hand-driving the Flask
slider. It is machine-precise with near-zero timing lag, so correlation is far
cleaner (≈0.99 vs ≈0.55 for a human ramp) even though it is coarser/sparser. Two
rules: (1) **always exclude the reference's source IDs** from the search
(`--exclude-ids`) so it can't self-match inside the reference frame; (2) the raw
bus may simply not carry the signal — if nothing correlates / verify is
UNCONFIRMED, say so rather than forcing a fit.
