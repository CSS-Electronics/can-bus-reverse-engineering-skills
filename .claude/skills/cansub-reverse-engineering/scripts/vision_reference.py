"""
vision_reference.py - digitize a reference signal from a VIDEO of a display.

Third reference source for the RE pipeline, alongside decode_reference.py (offline
machine reference) and flask_sync.py (live human reference). When the user has NO
decodable on-bus reference and CANNOT run a live capture, but CAN point a camera at a
display (dashboard, gauge app, instrument cluster) showing the true physical value,
this OCRs the displayed number per video frame into the SAME sidecar CSV the rest of
the pipeline consumes. correlate / bitsearch / build_dbc / verify then run UNCHANGED
against the raw CAN log - exactly like the OFFLINE workflow, just with no --exclude-ids
(the reference is off-bus, so it cannot self-match).

OCR is local + open source (RapidOCR, ONNX runtime, no PyTorch). v1 targets NUMERIC /
DIGITAL readouts (X degC, Y km/h, A %); analog needle gauges are out of scope.

TIME BASE: frame 0 is anchored to com.apple.quicktime.creationdate (iPhone CAPTURE-START
time, timezone-aware) + per-frame delta -> ABSOLUTE epoch per frame; the webCAN CSV is
also absolute, so they align directly. NB: the mvhd creation_time is often the
FILE-FINALIZE time (measured +9.5/+37/+15s after capture on real clips), so it is NOT
used when creationdate is present (only as a warned fallback). Residual is the device
clock sync error + creationdate's 1s quantization (~1-2s), absorbed by the lag search
(widen --max-lag for the video case). No reliable anchor (non-iPhone)? Film the webCAN
header clock briefly and use --measure-clock to get a --time-offset. Override the anchor
with --start-epoch / nudge with --time-offset.

TWO MODES:
  1) Frame-dump - sample a handful of frames spread across the clip so the ASSISTANT can
     inspect them, locate the digit region, and read the unit:
       python scripts/vision_reference.py --video TEMP/clip.mov --dump-frames 9
  2) Extract - OCR the chosen ROI across the clip into a sidecar (+ diagnostics + a
     0-vision-reference.png confirmation plot + a .meta.json for the review app):
       python scripts/vision_reference.py --video TEMP/clip.mov --roi 680,480,360,150 \
           --label speed_ref --unit km/h --fps 20

Then (just like the offline workflow, but WITHOUT --exclude-ids):
       python scripts/survey.py --trace <log.csv>
       python scripts/correlate.py --trace <log.csv> --sidecar temp-output/sidecar_speed_ref.csv \
           --type continuous --max-lag 2
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import common  # noqa: E402  (COLOR_REFERENCE + output conventions)

NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _parse_iso(ts: str):
    """Parse an ffprobe ISO timestamp (trailing 'Z' or '+0200' / '+02:00') -> aware dt."""
    s = ts.replace("Z", "+00:00")
    m = re.search(r"([+-]\d{2})(\d{2})$", s)        # +0200 -> +02:00 for fromisoformat
    if m:
        s = s[:m.start()] + f"{m.group(1)}:{m.group(2)}"
    return datetime.fromisoformat(s)


def probe_time_anchors(video: str) -> dict:
    """Both candidate frame-0 anchors from the container, as aware datetimes (or None).

    `com.apple.quicktime.creationdate` = iPhone CAPTURE-START time (timezone-aware) -
    the correct frame-0 anchor. `creation_time` (mvhd) is often the FILE-FINALIZE/save
    time, which can sit many (and VARIABLE) seconds after capture start - measured at
    +9.5/+37/+15 s on three real iPhone clips - so it is unreliable as an anchor.
    """
    res = {"creationdate": None, "creation_time": None}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video],
            capture_output=True, text=True, check=True,
        ).stdout
        tags = json.loads(out).get("format", {}).get("tags", {})
        for key, tag in (("creationdate", "com.apple.quicktime.creationdate"),
                         ("creation_time", "creation_time")):
            if tags.get(tag):
                try:
                    res[key] = _parse_iso(tags[tag])
                except ValueError:
                    pass
    except Exception as exc:  # noqa: BLE001
        print(f"  (ffprobe metadata unavailable: {exc})", file=sys.stderr)
    return res


def probe_creation_epoch(video: str):
    """Frame-0 anchor as a Unix epoch (s), PREFERRING the capture-start time.

    Order: `com.apple.quicktime.creationdate` (capture start) > `creation_time`
    (finalize, unreliable) > None. Warns when the two disagree (so the finalize-time
    trap is visible) and when only `creation_time` is present (non-iPhone container) -
    in that case verify the sync with a short webCAN clock-test clip (`--measure-clock`)
    and pass the recommended `--time-offset`, or set `--start-epoch` directly.
    Override either way with --start-epoch / --time-offset.
    """
    a = probe_time_anchors(video)
    cd, ct = a["creationdate"], a["creation_time"]
    if cd is not None:
        if ct is not None:
            d = ct.timestamp() - cd.timestamp()
            if abs(d) > 2.0:
                print(f"  anchor: using com.apple.quicktime.creationdate "
                      f"({cd.isoformat()}); creation_time is {d:+.0f}s off it (likely "
                      f"file-finalize time, not capture start) - ignoring it.")
        return cd.timestamp()
    if ct is not None:
        print(f"  [!] no com.apple.quicktime.creationdate (non-iPhone container?); "
              f"falling back to creation_time ({ct.isoformat()}), which may be the "
              f"file-finalize time, NOT capture start. If the decode won't align, record "
              f"a short webCAN clock-test clip and run --measure-clock to get a "
              f"--time-offset, or set --start-epoch.", file=sys.stderr)
        return ct.timestamp()
    return None


def parse_number(text: str):
    m = NUM_RE.search(text.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def open_video(path: str):
    import cv2
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        print(f"ERROR: could not open video: {path}", file=sys.stderr)
        sys.exit(1)
    return cap


def dump_frames(video: str, n: int, start_epoch) -> int:
    """Write N evenly-spaced frames for the assistant to inspect (ROI + unit)."""
    import cv2
    cap = open_video(video)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    dur = total / fps if fps else 0.0
    out_dir = Path("temp-output/vision-frames")
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in out_dir.glob("f*.png"):
        p.unlink()

    idxs = np.linspace(0, max(0, total - 1), num=max(2, n)).astype(int)
    print(f"video    : {video}")
    print(f"duration : {dur:.2f}s  ({total} frames @ {fps:.3f} fps)")
    if start_epoch:
        print(f"creation : {datetime.fromtimestamp(start_epoch, timezone.utc).isoformat()} "
              f"(epoch {start_epoch:.3f}) -> frame-0 anchor")
    print(f"\nWrote {len(idxs)} sample frames to {out_dir}/ :")
    for k, idx in enumerate(idxs):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if not ok:
            continue
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        fp = out_dir / f"f{k:02d}_t{t:06.2f}.png"
        cv2.imwrite(str(fp), frame)
        print(f"  {fp}   (t={t:.2f}s)")
    cap.release()
    print("\nNext: inspect these frames to (a) read the quantity + unit shown and "
          "(b) find the digit region, then re-run with\n"
          "  --roi <x>,<y>,<w>,<h> --label <signal>_ref --unit <unit>\n"
          "Pick an ROI generous enough to enclose the digits at EVERY value in the clip "
          "(the digit count changes, e.g. 9.7 -> 100.0).")
    return 0


def extract(args, start_epoch: float) -> int:
    import cv2
    from rapidocr import RapidOCR

    try:
        x, y, w, h = (int(v) for v in args.roi.split(","))
    except ValueError:
        print('ERROR: --roi must be "x,y,w,h" integers', file=sys.stderr)
        return 1

    cap = open_video(args.video)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    print(f"video      : {args.video}  ({src_fps:.3f} fps, ~{n_frames} frames)")
    print(f"ROI        : x={x} y={y} w={w} h={h}   target {args.fps} Hz")
    print(f"start epoch: {start_epoch:.6f}  (+ {args.time_offset:g}s offset)")

    ocr = RapidOCR()
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)) if args.clahe else None
    debug_dir = Path("temp-output/vision-frames-debug")
    if args.debug:
        debug_dir.mkdir(parents=True, exist_ok=True)

    step_ms = 1000.0 / args.fps
    next_ms = 0.0
    samples = []  # (epoch, frame_time_s, value_raw, confidence)
    sampled = read_ok = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        pos_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
        if pos_ms + 1e-6 < next_ms:
            continue
        next_ms += step_ms
        sampled += 1

        crop = frame[y:y + h, x:x + w]
        if crop.size == 0:
            continue
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        if args.upscale != 1.0:
            gray = cv2.resize(gray, None, fx=args.upscale, fy=args.upscale,
                              interpolation=cv2.INTER_CUBIC)
        # CLAHE (local contrast) beats global normalize on glary instrument-cluster
        # displays; fall back to MINMAX normalize when --no-clahe.
        gray = clahe.apply(gray) if clahe else cv2.normalize(gray, None, 0, 255,
                                                             cv2.NORM_MINMAX)
        ocr_img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

        # recognize mode: the ROI already localizes the digits, so skip the text
        # DETECTOR (which fails on big glary digits) and recognize the crop directly.
        if args.ocr_mode == "recognize":
            out = ocr(ocr_img, use_det=False, use_cls=False)
        else:
            out = ocr(ocr_img)
        value = conf = None
        if out.txts:
            scores = out.scores or [0.0] * len(out.txts)
            best = None
            for txt, score in zip(out.txts, scores):
                v = parse_number(txt)
                if v is not None and (best is None or score > best[1]):
                    best = (v, float(score))
            if best is not None:
                value, conf = best
                read_ok += 1

        frame_t = pos_ms / 1000.0
        samples.append((start_epoch + frame_t + args.time_offset, frame_t, value, conf))
        if args.debug:
            tag = f"{value}" if value is not None else "NA"
            cv2.imwrite(str(debug_dir / f"f{sampled:05d}_{tag}.png"), ocr_img)
        if sampled % 25 == 0:
            print(f"  ...{sampled} frames", end="\r")

    cap.release()
    if not samples:
        print("\nERROR: no frames sampled", file=sys.stderr)
        return 1

    return _finalize(args, samples, sampled, read_ok, start_epoch)


def _finalize(args, samples, sampled, read_ok, start_epoch) -> int:
    """Clean the per-frame OCR reads and write sidecar / diagnostics / meta / plot.

    Split out from extract() so the SAME post-processing runs from cached reads in
    --reclean (no re-OCR). All the cheap cleaning lives here; the expensive RapidOCR
    pass (or the cached diagnostics read) only produces `samples`.
    """
    eff_jump, jump_src = _resolve_max_jump(args, samples)
    cleaned, dropped, clamped, by_conf, by_jump = _clean(samples, args, eff_jump)
    label = args.label
    rows = [(e, v) for e, _ft, _raw, v, _c in cleaned if v is not None]
    if len(rows) < 3:
        print(f"\nERROR: only {len(rows)} confident reads - OCR could not track the "
              f"display. Check the ROI / try --conf lower.", file=sys.stderr)
        return 1

    # --- write sidecar (skill schema) -------------------------------------
    out = Path(args.out) if args.out else Path(f"temp-output/sidecar_{label}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("epoch;kind;label;value\n")
        for e, v in rows:
            f.write(f"{e:.6f};value;{label};{v:g}\n")

    # --- diagnostics ------------------------------------------------------
    diag = out.with_name(f"sidecar_{label}_diagnostics.csv")
    with open(diag, "w", encoding="utf-8") as f:
        f.write("epoch;frame_time_s;value_raw;value_clean;confidence\n")
        for e, ft, raw, v, c in cleaned:
            f.write(f"{e:.6f};{ft:.3f};{'' if raw is None else f'{raw:g}'};"
                    f"{'' if v is None else f'{v:g}'};{'' if c is None else f'{c:.3f}'}\n")

    # --- meta (for the review app) ----------------------------------------
    # roi may be absent on --reclean (we then preserve the prior meta's roi).
    roi = None
    if args.roi:
        try:
            roi = [int(v) for v in args.roi.split(",")]
        except ValueError:
            roi = None
    meta = out.with_name(f"sidecar_{label}.meta.json")
    if roi is None and meta.is_file():
        try:
            roi = json.loads(meta.read_text(encoding="utf-8")).get("roi")
        except Exception:  # noqa: BLE001
            roi = None
    meta.write_text(json.dumps({
        "video": str(Path(args.video).resolve()), "roi": roi,
        "start_epoch": start_epoch, "time_offset": args.time_offset,
        "fps": args.fps, "label": label, "unit": args.unit,
    }, indent=2), encoding="utf-8")

    # --- stats + coarse-ref warning ---------------------------------------
    es = np.array([e for e, _ in rows])
    vs = np.array([v for _, v in rows])
    span = float(es[-1] - es[0])
    rate = len(es) / span if span > 0 else 0.0
    distinct = int(len(np.unique(vs)))
    confs = [c for *_r, c in cleaned if c is not None]
    print(f"\nDigitized '{label}' [{args.unit}] from {Path(args.video).name}:")
    print(f"  OCR read rate {read_ok}/{sampled} ({100.0*read_ok/sampled:.1f}%)"
          f"   mean conf {np.mean(confs):.3f}" if confs else "")
    print(f"  n={len(vs)}  min={vs.min():.4g}  max={vs.max():.4g}  mean={vs.mean():.4g}  "
          f"span={span:.0f}s  rate={rate:.1f}Hz  distinct={distinct}  "
          f"(dropped {dropped}, clamped {clamped})")
    if jump_src == "adaptive" and eff_jump is not None:
        print(f"  auto-clean: rejected {by_jump} jump outlier(s) > {eff_jump:.3g} "
              f"{args.unit} from the running median (adaptive; --max-jump to override, "
              f"--no-auto-clean to disable), {by_conf} below conf {args.conf}")
    elif jump_src == "manual":
        print(f"  clean: rejected {by_jump} jump outlier(s) > {eff_jump:.3g} {args.unit} "
              f"(--max-jump), {by_conf} below conf {args.conf}")
    if distinct < 5 or rate < 1.0:
        print(f"  [!] coarse reference (distinct={distinct}, rate={rate:.1f}Hz) - "
              f"correlation may be unreliable.", file=sys.stderr)
    print(f"Wrote {out}  ({len(rows)} value rows)")
    print(f"Wrote {diag}")
    print(f"Wrote {meta}")

    # --- confirmation plot ------------------------------------------------
    png = Path(args.png) if args.png else Path("temp-output/0-vision-reference.png")
    png.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(11, 4))
    ax.step(es - es[0], vs, where="post", color=common.COLOR_REFERENCE)
    ax.set_xlabel("time (s, video-relative)")
    ax.set_ylabel(f"{label} [{args.unit}]")
    ax.set_title(f"Vision reference: {label}  "
                 f"({read_ok}/{sampled} frames read, mean conf "
                 f"{np.mean(confs):.2f})" if confs else f"Vision reference: {label}")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(png, dpi=110)
    print(f"Wrote {png}  - review this (and the review app) to confirm the reference.")

    # --- Next: guidance (offline workflow, but NO --exclude-ids) -----------
    print(f"\nReview it against the video, then search the RAW bus (off-bus reference, "
          f"so NO --exclude-ids):\n"
          f"  python scripts/vision_review.py --video {args.video} --sidecar {out}\n"
          f"  python scripts/survey.py --trace <log.csv>\n"
          f"  python scripts/correlate.py --trace <log.csv> --sidecar {out} "
          f"--type continuous --max-lag 2")
    return 0


def _clean(samples, args, max_jump):
    """Confidence floor + running-median jump reject + optional clamp.

    `max_jump` is the resolved threshold (manual --max-jump, the adaptive default,
    or None to disable jump-reject) - see _resolve_max_jump. The running-median
    reject is what catches BOTH far-out OCR spikes (a glare misread to 200) AND
    momentary dropouts (a digit lost to motion-blur reading 0 mid-drive): both
    deviate hard from the local median, neither survives. Non-destructive: keeps raw
    alongside cleaned so the diagnostics CSV stays auditable (and re-cleanable).
    """
    def med(vals):
        s = sorted(v for v in vals if v is not None)
        return s[len(s) // 2] if s else None

    cleaned, dropped, clamped, by_conf, by_jump = [], 0, 0, 0, 0
    recent = []
    for e, ft, raw, conf in samples:
        v = raw
        if v is not None and conf is not None and conf < args.conf:
            v, by_conf = None, by_conf + 1
        if v is not None and max_jump is not None and recent:
            m = med(recent[-7:])
            if m is not None and abs(v - m) > max_jump:
                v, by_jump = None, by_jump + 1
        if v is not None and args.vmin is not None and v < args.vmin:
            v, clamped = args.vmin, clamped + 1
        if v is not None and args.vmax is not None and v > args.vmax:
            v, clamped = args.vmax, clamped + 1
        if v is None and raw is not None:
            dropped += 1
        if v is not None:
            recent.append(v)
        cleaned.append((e, ft, raw, v, conf))
    return cleaned, dropped, clamped, by_conf, by_jump


def _adaptive_max_jump(raw_reads):
    """A unit-agnostic running-median jump threshold derived from the data itself.

    A fixed --max-jump is signal-specific (15 km/h is right for speed, wrong for RPM
    or coolant temp), so it can't be a sensible default. Bound the per-frame step
    instead by a fraction of the signal's OWN robust range: a real analog readout
    moves smoothly at video frame-rate, so the 7-sample running median tracks any
    genuine transition and a single frame deviating by a large fraction of the full
    range is almost certainly an OCR misread (a glare spike, or a dropped/!misread
    digit). The fraction-of-range floor is the workhorse; a MAD-based slew term only
    lifts the threshold for a genuinely fast/noisy signal whose real per-frame deltas
    are large. The 0.18 floor was tuned on a real dashboard-speed clip (it lands mid
    plateau of the threshold sweep, reproducing a hand-picked --max-jump 15 without
    biasing the kept-sample mean); p2/p98 trims a sub-2% spike cluster so the range
    isn't inflated by the very outliers we mean to reject. Returns None when there
    are too few reads to estimate (jump-reject is then simply skipped).
    """
    vals = np.array([r for r in raw_reads if r is not None], dtype=np.float64)
    if vals.size < 10:
        return None
    d = np.abs(np.diff(vals))
    if d.size == 0:
        return None
    med_d = float(np.median(d))
    mad_d = float(np.median(np.abs(d - med_d)))
    slew = med_d + 5.0 * 1.4826 * mad_d          # robust per-frame slew budget
    rng = float(np.percentile(vals, 98) - np.percentile(vals, 2))
    thr = max(6.0 * slew, 0.18 * rng)
    return thr if thr > 0 else None


def _resolve_max_jump(args, samples):
    """Pick the effective jump threshold and report its source.

    Precedence: explicit --max-jump (manual) > adaptive default (auto-clean on) >
    None (--no-auto-clean, or too few reads). Returns (threshold, source-tag).
    """
    if args.max_jump is not None:
        return args.max_jump, "manual"
    if getattr(args, "auto_clean", True):
        eff = _adaptive_max_jump([raw for _e, _ft, raw, _c in samples])
        return eff, ("adaptive" if eff is not None else "off")
    return None, "off"


def _read_diagnostics(path: Path):
    """Reconstruct per-frame OCR `samples` from a diagnostics CSV (for --reclean).

    The diagnostics CSV already caches every raw OCR read + confidence, so a re-clean
    (tighter --conf, a --min/--max clamp, --max-jump) never has to re-run the
    expensive OCR pass over the video - it just re-derives value_clean from value_raw.
    """
    samples = []
    sampled = read_ok = 0
    with open(path, encoding="utf-8") as f:
        header = f.readline()
        if "value_raw" not in header:
            raise ValueError(f"{path} is not a vision diagnostics CSV")
        for line in f:
            parts = line.rstrip("\n").split(";")
            if len(parts) < 5:
                continue
            e, ft, raw, _clean, conf = parts[:5]
            sampled += 1
            raw_v = float(raw) if raw != "" else None
            conf_v = float(conf) if conf != "" else None
            if raw_v is not None:
                read_ok += 1
            samples.append((float(e), float(ft), raw_v, conf_v))
    return samples, sampled, read_ok


def _reclean(args) -> int:
    """Re-derive the sidecar from cached OCR reads WITHOUT re-running OCR.

    `--reclean [diagnostics.csv]` re-applies cleaning (conf floor, adaptive/manual
    jump-reject, clamps) to the cached raw reads and rewrites the sidecar / diagnostics
    / meta / plot. Turns a "re-tune the cleaning" round from a multi-minute re-OCR into
    an instant pass. Path defaults to the diagnostics for --label/--out.
    """
    if isinstance(args.reclean, str) and args.reclean:
        diag = Path(args.reclean)
    else:
        out = Path(args.out) if args.out else Path(f"temp-output/sidecar_{args.label}.csv")
        diag = out.with_name(f"sidecar_{args.label}_diagnostics.csv")
    if not diag.is_file():
        print(f"ERROR: --reclean diagnostics CSV not found: {diag}\n"
              f"  run an OCR pass first, or pass --reclean <path> / --label / --out.",
              file=sys.stderr)
        return 1
    print(f"reclean    : {diag}  (re-applying cleaning, NO re-OCR)")
    samples, sampled, read_ok = _read_diagnostics(diag)
    if not samples:
        print("ERROR: no rows in diagnostics CSV", file=sys.stderr)
        return 1
    # start_epoch is only needed for meta; recover it from the first cached frame.
    e0, ft0, *_ = samples[0]
    start_epoch = e0 - ft0 - args.time_offset
    return _finalize(args, samples, sampled, read_ok, start_epoch)


def measure_clock(args, start_epoch: float) -> int:
    """Measure the anchor-vs-true-clock offset from a short webCAN clock-test clip.

    When the container has no reliable capture-start anchor (a non-iPhone camera, or a
    re-wrapped file), film the webCAN HEADER CLOCK (the HH:MM:SS it shows) for a few
    seconds. This OCRs that clock, finds the 1 Hz tick edges, and reports how far the
    video-derived epoch (anchor + frame_t) sits from the true wall-clock - i.e. the
    `--time-offset` to pass when extracting the REAL clip (recorded in the same session,
    so the same offset applies). The displayed time is local; we interpret it in the
    anchor's timezone (iPhone `creationdate` carries it) unless --display-utc-offset is
    given (use it for a non-iPhone clock-test, e.g. `--display-utc-offset 2` for CEST).
    """
    import cv2
    from rapidocr import RapidOCR

    try:
        x, y, w, h = (int(v) for v in args.measure_clock.split(","))
    except ValueError:
        print('ERROR: --measure-clock must be "x,y,w,h" integers', file=sys.stderr)
        return 1

    anchors = probe_time_anchors(args.video)
    anchor_dt = anchors["creationdate"] or anchors["creation_time"]
    if anchor_dt is None and args.start_epoch is None:
        print("ERROR: no usable anchor (no creationdate/creation_time and no "
              "--start-epoch) to measure against.", file=sys.stderr)
        return 1
    anchor_epoch = start_epoch
    if args.display_utc_offset is not None:
        disp_tz = timezone(timedelta(hours=args.display_utc_offset))
    elif anchor_dt is not None and anchor_dt.utcoffset() is not None:
        disp_tz = anchor_dt.tzinfo
    else:
        disp_tz = timezone.utc
        print("  [!] display timezone unknown; assuming UTC. Pass --display-utc-offset "
              "<hours> (the webCAN clock is LOCAL time) if the offset looks ~whole-hours "
              "wrong.", file=sys.stderr)
    disp_date = (anchor_dt.astimezone(disp_tz).date() if anchor_dt is not None
                 else datetime.fromtimestamp(anchor_epoch, disp_tz).date())
    print(f"clock-test : {args.video}")
    print(f"  anchor   : {anchor_epoch:.3f} ({'creationdate' if anchors['creationdate'] else 'creation_time/start-epoch'}); display tz {disp_tz}")

    time_re = re.compile(r"(\d{1,2})\D(\d{2})\D(\d{2})")
    ocr = RapidOCR()
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    cap = open_video(args.video)
    prev, offsets, ticks = None, [], []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        crop = frame[y:y + h, x:x + w]
        if crop.size == 0:
            continue
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        gray = clahe.apply(gray)
        res = ocr(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), use_det=False, use_cls=False)
        if not res.txts:
            continue
        m = time_re.search(res.txts[0].replace(" ", ""))
        if not m:
            continue
        hh, mm, ss = (int(g) for g in m.groups())
        if hh > 23 or mm > 59 or ss > 59:
            continue
        disp = f"{hh:02d}:{mm:02d}:{ss:02d}"
        if disp != prev:
            if prev is not None:                     # genuine rising edge only
                webcan = datetime(disp_date.year, disp_date.month, disp_date.day,
                                  hh, mm, ss, tzinfo=disp_tz).timestamp()
                offsets.append(anchor_epoch + t - webcan)
                ticks.append((t, disp))
            prev = disp
    cap.release()

    if len(offsets) < 1:
        print("ERROR: could not read the webCAN clock (no 1 Hz tick edges). Check the "
              "ROI with --dump-frames; the clock must be a HH:MM:SS readout.",
              file=sys.stderr)
        return 1
    mean = sum(offsets) / len(offsets)
    spread = max(offsets) - min(offsets)
    print(f"  ticks    : {len(ticks)} ({ticks[0][1]} .. {ticks[-1][1]})")
    print(f"  offset   : mean {mean:+.3f}s  spread {spread:.3f}s  "
          f"(video epoch - webCAN clock; + => video anchor AHEAD)")
    if abs(mean) <= 2.0:
        print(f"  => within +/-2s already; no --time-offset needed (lag search absorbs "
              f"{mean:+.2f}s).")
    else:
        print(f"  => pass  --time-offset {-mean:.2f}  when extracting the real clip "
              f"(same session) to bring it within the lag window.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", required=True, help="input video of the display")
    ap.add_argument("--dump-frames", nargs="?", type=int, const=9, default=None,
                    metavar="N", help="write N (default 9) sample frames for ROI/unit "
                    "inspection and stop")
    ap.add_argument("--roi", help='digit region "x,y,w,h" (from inspecting --dump-frames)')
    ap.add_argument("--label", default="vision_ref", help="sidecar label / signal name")
    ap.add_argument("--unit", default="", help="physical unit shown on the display (km/h, degC, %%)")
    ap.add_argument("--fps", type=float, default=20.0, help="sample rate Hz (default 20; floor 1)")
    ap.add_argument("--start-epoch", type=float, default=None,
                    help="Unix epoch of frame 0 (default = video creation_time)")
    ap.add_argument("--time-offset", type=float, default=0.0,
                    help="seconds added to every epoch (manual sync nudge)")
    ap.add_argument("--out", help="sidecar CSV (default temp-output/sidecar_<label>.csv)")
    ap.add_argument("--png", help="confirmation plot (default temp-output/0-vision-reference.png)")
    ap.add_argument("--min", dest="vmin", type=float, default=None, help="clamp minimum")
    ap.add_argument("--max", dest="vmax", type=float, default=None, help="clamp maximum")
    ap.add_argument("--max-jump", type=float, default=None,
                    help="reject reads deviating > this from the running median "
                         "(overrides the adaptive auto-clean default)")
    ap.add_argument("--no-auto-clean", dest="auto_clean", action="store_false",
                    help="disable the adaptive jump-outlier reject (keep raw reads "
                         "unless --max-jump/--conf/--min/--max are set explicitly)")
    ap.set_defaults(auto_clean=True)
    ap.add_argument("--reclean", nargs="?", const="", default=None, metavar="DIAG_CSV",
                    help="re-derive the sidecar from a cached diagnostics CSV WITHOUT "
                         "re-running OCR (re-applies cleaning only). Defaults to the "
                         "diagnostics for --label/--out; pass a path to point elsewhere.")
    ap.add_argument("--measure-clock", metavar="X,Y,W,H", default=None,
                    help="clock-test mode: OCR the webCAN HEADER CLOCK in this ROI and "
                         "report the anchor-vs-true-time offset (and the --time-offset to "
                         "apply to the real clip). Use when the container has no reliable "
                         "capture-start anchor.")
    ap.add_argument("--display-utc-offset", type=float, default=None, metavar="H",
                    help="UTC offset (hours) of the webCAN clock shown in a --measure-clock "
                         "clip, when it can't be inferred from the anchor (non-iPhone).")
    ap.add_argument("--conf", type=float, default=0.3, help="drop OCR reads below this (0..1)")
    ap.add_argument("--upscale", type=float, default=3.0, help="ROI upscale before OCR")
    ap.add_argument("--ocr-mode", choices=["recognize", "detect"], default="recognize",
                    help="recognize (default): the ROI localizes the digits, skip the text "
                         "detector and recognize the crop directly - robust on big/glary "
                         "displays. detect: full detect+recognize, for an ROI that still "
                         "contains extra text to locate.")
    ap.add_argument("--no-clahe", dest="clahe", action="store_false",
                    help="use global MINMAX normalize instead of CLAHE local contrast")
    ap.set_defaults(clahe=True)
    ap.add_argument("--debug", action="store_true", help="dump OCR crops to temp-output/")
    args = ap.parse_args()

    args.fps = max(1.0, args.fps)

    # --reclean reuses cached OCR reads from the diagnostics CSV, so the video is
    # NOT opened (it need not even still be on disk).
    if args.reclean is not None:
        return _reclean(args)

    if not Path(args.video).is_file():
        print(f"ERROR: video not found: {args.video}", file=sys.stderr)
        return 1

    start_epoch = args.start_epoch
    if start_epoch is None:
        start_epoch = probe_creation_epoch(args.video)
    if start_epoch is None:
        start_epoch = 0.0
        print("  (no creation_time; start-epoch=0 -> epochs are video-relative. Pass "
              "--start-epoch to align with the CAN log.)", file=sys.stderr)

    if args.measure_clock is not None:
        return measure_clock(args, start_epoch)
    if args.dump_frames is not None:
        return dump_frames(args.video, args.dump_frames, start_epoch)
    if not args.roi:
        print("ERROR: pass --dump-frames to inspect, or --roi x,y,w,h to extract.",
              file=sys.stderr)
        return 1
    return extract(args, start_epoch)


if __name__ == "__main__":
    raise SystemExit(main())
