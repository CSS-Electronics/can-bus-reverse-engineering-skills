# CAN signal encoding reference

How raw bits in a CAN payload map to a physical value, and how that maps to a
cantools/DBC `Signal`. The skill's extraction must agree with cantools exactly —
`python scripts/common.py --selftest` checks this.

## Physical value formula

    physical = offset + scale * raw

- `raw` = integer extracted from the chosen bitfield (optionally two's-complement
  if `signed`).
- For **discrete** signals scale=1, offset=0 is usually right.
- For **continuous** signals derive scale then offset (see build_dbc.py).

## Signal min/max are THEORETICAL, not observed

The DBC `minimum`/`maximum` describe the field's full **representable** range —
every value the bits can encode — not what a particular log happened to contain
(a car parked the whole trip doesn't make its top speed 0, and a short drive to
70 km/h doesn't cap the signal at 70). Compute them from the raw code range mapped
through `offset + scale*raw`:

- unsigned: raw `0 .. 2^length − 1`
- signed:   raw `−2^(length−1) .. 2^(length−1) − 1`
- a negative scale flips the endpoints (min ↔ max).

So a 16-bit unsigned speed at scale `0.1`, offset `0` declares `0 .. 6553.5`, not
`0 .. 72.1`. `common.theoretical_range()` computes this and is the default used by
`make_single_signal_db` (build_dbc / calibrate). (The *observed* range still
matters elsewhere — e.g. extreme-outlier detection — just not for the declared
signal limits.)

## Bit / byte order (the two DBC conventions)

A CAN payload is bytes `D0 D1 D2 ...`. cantools uses **sawtooth bit numbering**
with `start` = the LSB position for little-endian and the MSB position for
big-endian.

- **Little-endian (Intel):** value is little-endian across bytes. Global bit
  index `k = byte*8 + bit_in_byte` with `bit_in_byte` 0 = LSB. A field of
  `length` bits with cantools `start = S` (`byte_order='little_endian'`) is:

      le_int = int.from_bytes(payload, 'little')
      raw    = (le_int >> S) & ((1 << length) - 1)

  This is what the skill's `extract_le()` computes; cantools `start = S`.

- **Big-endian (Motorola):** bytes are MSB-first. The skill restricts the
  *search* to **byte-aligned** big-endian fields (the common case for continuous
  signals on clean borders). For a field at byte offset `b`, width `w` bytes, in
  a payload of length `L`:

      be_int = int.from_bytes(payload, 'big')
      raw    = (be_int >> (8 * (L - b - w))) & ((1 << (8*w)) - 1)

  cantools mapping: `byte_order='big_endian'`, `length = 8*w`,
  `start = b*8 + 7` (MSB of the first byte in sawtooth numbering).

## Conventions OEMs tend to follow (priors for the search)

- Booleans / flags: 1–2 bits.
- Continuous signals: 1–2 bytes, frequently on a clean byte border (not split
  awkwardly across a byte boundary), big- or little-endian.
- High-precision continuous: up to 4 bytes.
- A signal's value range should be physically plausible (e.g. speed 0–250 km/h).

## Signedness

`signed = True` → interpret the `length`-bit `raw` as two's complement:

    if raw >= (1 << (length-1)): raw -= (1 << length)

cantools: `is_signed=True`.

## Bit-packed frames (don't assume byte alignment)

Compact sensor frames often pack several **sub-byte** fields with no byte
alignment, sometimes behind a leading flag bit. Example (a real IMU frame, 8
bytes): `1 (valid) + 10 + 10 + 10 (accel X/Y/Z) + 11 + 11 + 11 (gyro X/Y/Z)` = 64
bits. Here Acceleration X is **bits 1–10, little-endian, unsigned** (scale 0.125,
offset −64) — NOT bytes 0–1. A byte-aligned read of bytes 0–1 mixes the flag bit,
all of X, and part of Y: a scrambled function of the truth that still *correlates*.
`survey.py` proposes the field map (LSB-first start bits) and flags the leading
bit; `bitsearch.py` finds the exact field. Always bit-search a continuous signal;
never ship a byte guess.

## Reference-free plausibility prior

Every candidate field is also scored by a prior that needs **no reference**: the
decoded raw series, in frame order, must be physically plausible — **smooth**
(autocorrelated frame-to-frame) and **wrap-free** (no per-frame deltas near
±2^length, which betray a wrong start bit / length / endianness, or a slice
straddling a real field boundary). A correct field scores high; a scrambled one
scores low however well it happens to correlate. Combined with lag-aligned
linear-R², this is what ranks the true field above its sub-slices (`common.plausibility`).

## Sentinels / out-of-band outliers (agnostic detection)

A field with no valid value to report transmits a **sentinel** that decodes **far
outside** the physical band — "signal invalid / unavailable / not initialised". It
is often a maxed-out / all-ones code (`0xFFFF` at scale `0.1` → `6553.5 km/h`) but
**not always**: a signal-unavailable RPM can read `0x3FFF` (a
*14-bit* all-ones inside a 16-bit read), alongside neighbouring codes like
`0x4000`/`0x4041`, in a few percent of frames. So detection must **not** assume the sentinel is all-ones, a single
value, a small fraction, or on the high side. These are not "high readings" (for
speed an outlier is `>300 km/h`, not `>75`); they distort a least-squares fit, the
declared DBC range, the scoring and any plot's Y axis. A robust fit (Theil-Sen +
median) survives a few, but they must be excluded.

`common.detect_extreme_outliers` is **agnostic**, resting on four scale-free
pillars (the value's bit pattern is only a *confidence* hint, never a gate):

1. **Robust band** — `median ± k·MAD` (1.4826-scaled, IQR fallback). The MAD is
   unaffected by up to ~50% contamination, so a 4–10% cluster doesn't move the band
   (a p2/p98 band's 98th percentile could sit *on* the cluster — the old bug).
2. **Clear empty gap** — the out-of-band cluster must be separated from the bulk by
   an empty gap (≥ `gap_mad` MAD-units), tested **per side** (high *and* low, so a
   signed most-negative `0x8000` sentinel is caught). A gapless full-range
   continuous signal qualifies on neither side → nothing flagged. This is the
   primary false-positive guard.
3. **Teleport** — each contiguous out-of-band run ("episode") must be entered or
   left by a per-frame step far larger than the robust in-band slew budget. A
   sentinel teleports in one frame; a real signal ramps. (Uses timestamps for a
   true slew rate when available.)
4. **Episodes + sanity ceiling** — count contiguous runs, not raw frames (a long
   engine-off stretch is a couple of episodes); bail only above a high
   `sanity_frac`. There is **no 2% frequency cap** anymore.

It returns a `confidence` (high/medium/low) and `kind` (sentinel/outlier/suspect).
The classic all-ones / top / most-negative codes are detected via `near_structural`
and only *raise* confidence. **How it is wired in:**

- **Identification** (`correlate`, `bitsearch`) calls `common.auto_mask_outliers`
  to mask **high-confidence** detections (≤15% of frames) *before* scoring, so a
  sentinel can't wreck a candidate's linear R² and hand the win to a coarse slice.
  The high-confidence + fraction gate means a wrong
  field's diffuse scatter — which has no clean gap and no isolated teleport — is
  never masked.
- **Deliverables** (`build_dbc`, `verify`, `calibrate`) keep the human in the loop:
  they print the warning and take `--drop-extreme` (calibrate excludes them from
  each anchor median unless `--keep-extreme`). `verify` additionally **gates on the
  clean subset** at high confidence — excluding sentinels from the verdict even
  without `--drop-extreme` so a correct field can't false-FAIL on kept sentinels —
  while still drawing them on the plot.

Residual caveat: a genuinely **bistable** real signal (two far-apart states) can
look sentinel-like; a wide/balanced split never reaches "high" confidence (so
identification won't mask it) and deliverables still ask, but eyeball such cases.

## OEM scales are round (and get auto-snapped)

A fitted scale that isn't near a "nice" value — `m×10^k` (m ∈ {1, 2, 2.5, 5}) or
`2^−j` — is a strong hint the geometry is wrong. `0.125`, `0.001`, `1e-6`, `0.25`
are nice; an oddball like `1/2593 ≈ 3.86e-4` is flagged by build_dbc / calibrate.
Genuine high-precision scales (e.g. `1e-7` for lat/long) are nice and not flagged
(`common.scale_plausibility`).

**Auto-round (gated on systematic bias, NOT R²).** OEMs use a round scale *and* a
round offset (very often 0). A robust fit against a **quantized** reference lands
*near* those values, not exactly on them (integer-km/h OBD2 pulls a true `0.1` to
`~0.0999`). `common.propose_round_calibration` snaps the scale to its nearest nice
value and the offset to 0 / nearest integer, then measures the **worst-case
systematic bias** the snap introduces vs the precise fit, `max|Δscale·raw + Δoffset|`
over the observed data, as a fraction of the signal's range. It **auto-applies only
when that bias is ≤ ~1%** (a real noise-level cleanup); a snap that would inject
more is returned with `auto=False` — reported as a suggestion and **left for the
user**, not applied.

Why not gate on R²? Because **R² and rank correlation are nearly blind to a small
multiplicative scale error** — a 2–3% slope change keeps R² ≈ 0.99 (and Spearman
≈ 1). So an R²-drop gate happily snaps `0.0983 → 0.1` even when that reads 3% high
throughout (real scale ≈ 0.098, not 0.1; snapping injects a
visible bias). Gating on the bias budget catches exactly this. A genuine non-zero
offset (coolant temp `−40`) is still kept — zeroing it blows the bias budget — while
a true-`0.1` signal whose fit landed at `0.0999` snaps cleanly.

This pairs with verify's **`abs. agreement`** line (mean signed bias + decoded-vs-
reference slope), computed *without* an affine refit, so a systematic bias is
surfaced and `[!]`-flagged even on a high-Spearman PASS. (`residual_summary` /
`norm_resid` affine-refit and therefore measure *shape* only — they will not show a
scale/offset bias; that's by design, but it's why the absolute check exists.) A
flagged bias is a prompt to decide *non-round true scale* vs *biased reference*
(e.g. dashboard/indicated speed vs OBD true speed) — not an automatic failure.
`--no-round` disables snapping entirely.

## Physical anchors: a decode must be right at a known operating point

Auto-round cleans up *near-round* scale/offset. A second, often stronger correction
comes from **physical anchors** — operating points where the true physical value is
*known a priori*, independent of the reference. The near-universal one is the
**rest / zero state**: vehicle speed, mass-air-flow, current, power, torque all read
**exactly 0** when the system is idle. Others are signal-specific (engine coolant
starts near ambient; a gear selector rests in P/N; a throttle rests at its idle %).

Why this matters even at high R²: the fit minimises *overall* squared error, and
R²/Spearman are **blind to a constant offset** (just as they're blind to a few-%
scale error — see above). Against a **quantized and/or laggy** reference — e.g.
integer-km/h OBD speed that lags the fast raw frame through accel/decel transients —
the low end is biased, and a free two-parameter fit absorbs that into a small
non-zero offset. The decode then reads e.g. **−1.6 km/h while parked**: a ~2%-of-
range error that barely dents R² (`0.9984 → 0.9981`) yet is *physically impossible*.
A known anchor is exactly the constraint the blind fit lacks.

**Anchor re-fit (gated on the anchor disagreement).** `common.propose_anchor_calibration`
finds the reference's dense **rest cluster** and the **modal** raw value there (the
mode, not the median — a loose rest band catches low-speed creep frames whose raw is
non-zero and scattered; at a true steady state the field sits at one constant value,
so it dominates as the mode while the median gets dragged off it). It then **re-fits
the line constrained to pass through the anchor** `(raw_rest, anchor_value)` — least-
squares of `(ref − y₀)` on `(raw − x₀)`, which re-derives the *slope* from the data
while honouring the anchor exactly. This is deliberately **not** a pure offset shift:
shifting only the offset keeps a slope that was fitted for the *old* offset, so the
decode then reads systematically biased across the moving range (a parked-correct
line that reads ~2% high in motion). Re-deriving the slope removes that.

The gate mirrors auto-round's bias budget, but on the **anchor disagreement** (how
far the free fit's decode sits from the anchor, as a fraction of range):
- **small (≤ ~6%)** → a noise-level calibration cleanup → **auto-applied**;
- **large** → **flagged, not applied**: a big miss at a *certain* point means the
  field geometry is probably wrong (wrong slice/endianness) — loop back to bitsearch
  rather than papering a bad field over with an anchor.

Only a **true-zero** rest is auto-detected (the rest level must sit within ~5% of
range of 0); a non-zero anchor is signal-specific and must be declared with
`--anchor <value>` (which also forces the re-fit through that point). `--no-anchor`
disables the step. An honoured-anchor decode often still shows a small residual
`systematic bias` in build_dbc / verify — that's the **irreducible** reference
quantization + lag, now surfaced honestly instead of hidden inside a bogus offset
(don't chase it to zero by un-pinning the anchor). As with round-vs-precise, if the
*reference itself* is the biased one (indicated-vs-true speed), the user decides;
force any line with `--scale X --offset Y`.

**Caveat — scale-roundness can't catch an over-wide read of a binary-fraction
field.** Appending whole *bytes* to a field divides its scale by `256 = 2^8` per
byte. If the true scale is a binary fraction `2^−j` (very common: `1/64`, `1/128`),
the over-wide scale `2^−(j+8k)` is *still* a binary fraction — still "nice". So a
non-round scale is a one-way hint: non-round ⇒ probably wrong, but round does NOT
prove the width is right. The over-wide case is caught by parsimony instead (next).

## Over-wide reads: two adjacent fields read as one

Vehicles often broadcast two co-varying quantities side by side — a wheel-speed
**pair** (`[front_left | front_right]`), an L/R or X/Y axis, a value plus its
redundant copy. Read together as one wider field they still correlate almost
perfectly with the reference (both halves track it), so a naive width-greedy search
locks onto the *concatenation*: e.g. bytes 0–1 hold speed at scale `1/64`, but a
32-bit read of bytes 0–3 scores an equal R² with scale `1/64 ÷ 2^16 = 2^−22`. That
DBC decodes the current log fine yet is semantically wrong and **fragile** — the
moment the two halves diverge (one wheel slips, a turn) the concatenation glitches.

The tell is the **R²-vs-width knee**: R² *jumps* when you add the field's own low
byte, then *plateaus* once you spill into the neighbouring field (the extra bits add
no fit). The true width is the shortest one at the plateau. `bitsearch.py` encodes
this as a **parsimony** rule in `_suppress_overlaps`: among nested candidates that
fit equally well (R² within ε, no less plausible), it keeps the **shortest** — and
reports its real scale. A too-narrow high-byte slice fits measurably worse (R² drops
well past ε), so it is never preferred; a wrapping low slice is rejected by the
plausibility prior. You can also bound the search directly: `--max-len`/`--min-len`
now constrain **both** little- and big-endian widths (default `--max-len 24`
excludes 32-bit big-endian reads; raise it for a genuine 32-bit signal).

## Visual aids (analysis-plots/)

Each step auto-emits a brand-styled PNG into the signal's `analysis-plots/` folder
(see SKILL.md "Output structure"). They make the concepts above legible at a glance:
the **bus bit-activity heatmap** (survey) shades each ID's bits by flip-rate in the
**LSB-first** layout used everywhere here — dynamic bytes light up, counters/checksums
and constant flag bits are annotated, so candidate-rich IDs and related signals jump
out. The **R²-vs-width knee** described above is drawn directly in the bitsearch grid
PNG (the winner's nested family, jump-then-plateau), and the **fit diagnostic** shows
the raw→reference line with any rejected round candidate in red — the visual twin of
the auto-round bias gate.
