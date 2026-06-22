"""
Shared helpers for the cansub-reverse-engineering skill.

Covers: CANsub discovery, passive bit-rate probing, webCAN-CSV trace loading,
per-ID frame grouping, vectorized bitfield extraction (little-/big-endian),
cantools single-signal DBC mapping, and sidecar-reference loading.

All bus access here is passive (listen_only=True) — nothing is transmitted.

Run `python common.py --selftest` to validate bitfield extraction against
cantools without any hardware.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

import can
import cantools
import python_can_cansub  # noqa: F401  (registers the 'cansub' interface + CSV codec)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The CanSub constructor always needs a (valid) FD data bit-rate even on a
# classical bus, where it is never actually used for RX. Keep it >= any nominal.
DEFAULT_DATA_BITRATE = 1_000_000

# CANsub CAN controller clock.
CAN_F_CLOCK = 80_000_000

# Per-attempt listen window for auto-detect (seconds). Mirrors webCAN's
# AUTO_DETECT_TIMEOUT_MS.
AUTO_DETECT_TIMEOUT_S = 1.0

# Ordered bit-rate sequence for auto-detection, mirroring webCAN's
# AUTO_DETECT_SEQUENCE (constants.ts). Classical CAN first (data=1M is unused on
# RX of classical frames), then CAN FD nominal/data permutations. Each entry is
# {nominal, data, sample_point}; all validated against BitTimingFd @ 80 MHz.
AUTO_DETECT_SEQUENCE = [
    # Classical CAN
    {"nominal": 250_000,  "data": 1_000_000, "sample_point": 80},
    {"nominal": 500_000,  "data": 1_000_000, "sample_point": 80},
    {"nominal": 1_000_000, "data": 1_000_000, "sample_point": 80},
    {"nominal": 800_000,  "data": 1_000_000, "sample_point": 80},
    {"nominal": 200_000,  "data": 1_000_000, "sample_point": 80},
    {"nominal": 125_000,  "data": 1_000_000, "sample_point": 80},
    {"nominal": 100_000,  "data": 1_000_000, "sample_point": 80},
    {"nominal": 50_000,   "data": 1_000_000, "sample_point": 80},
    # CAN FD - 500K nominal with higher data rates
    {"nominal": 500_000,  "data": 2_000_000, "sample_point": 80},
    {"nominal": 500_000,  "data": 4_000_000, "sample_point": 80},
    {"nominal": 500_000,  "data": 5_000_000, "sample_point": 81.25},
    # CAN FD - 1M nominal with higher data rates
    {"nominal": 1_000_000, "data": 2_000_000, "sample_point": 80},
    {"nominal": 1_000_000, "data": 4_000_000, "sample_point": 80},
    {"nominal": 1_000_000, "data": 5_000_000, "sample_point": 81.25},
]


def make_timing(nominal: int, data: int, sample_point: float = 80.0):
    """Build a CANsub-compatible BitTimingFd from nominal/data rate + sample point."""
    return can.BitTimingFd.from_sample_point(
        f_clock=CAN_F_CLOCK,
        nom_bitrate=nominal, nom_sample_point=sample_point,
        data_bitrate=data, data_sample_point=sample_point,
    )


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------

def detect_configs(device: str | None = None) -> list[dict]:
    """Return python-can configs for connected CANsub channels (via mDNS).

    If `device` is given, keep only channels whose hostname contains it
    (e.g. the 8-char device id, or 'usb' / 'eth').
    """
    configs = can.detect_available_configs(interfaces=["cansub"])
    if device:
        configs = [c for c in configs if device.lower() in c["channel"].lower()]
    return configs


def pick_config(device: str | None = None, channel: int | None = None) -> dict:
    """Pick a single config, optionally filtered by device substring / channel."""
    configs = detect_configs(device)
    if not configs:
        raise RuntimeError(
            "No CANsub found via mDNS. Is the device connected and powered? "
            "(USB/Ethernet) — try again, or pass --device."
        )
    if channel is not None:
        configs = [c for c in configs if c["channel"].endswith(f"@{channel}")]
        if not configs:
            raise RuntimeError(f"No CANsub channel @{channel} found.")
    return configs[0]


# ---------------------------------------------------------------------------
# Bit-rate probing (passive)
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    attempt: dict          # {nominal, data, sample_point}
    outcome: str           # 'valid' | 'error' | 'timeout'


def _fmt_rate(hz: int) -> str:
    return f"{hz/1_000_000:g}M" if hz >= 1_000_000 else f"{hz/1000:g}k"


def probe_bitrate(
    config: dict,
    sequence: list[dict] | None = None,
    timeout_s: float = AUTO_DETECT_TIMEOUT_S,
) -> tuple[dict | None, list[ProbeResult]]:
    """Passively auto-detect the bus bit-rate (classical + CAN FD).

    Mirrors webCAN's BitrateAutoDetectService: for each (nominal, data,
    sample_point) attempt, open the bus listen_only + error_frames and listen up
    to `timeout_s`. The first *valid* (non-error) frame means the timing matches
    -> success. Error frames alone mean the bus is active but the rate is wrong
    (keep trying). No frames at all -> timeout. Returns (winning_attempt|None,
    results).
    """
    sequence = sequence or AUTO_DETECT_SEQUENCE
    results: list[ProbeResult] = []
    total = len(sequence)
    for i, attempt in enumerate(sequence):
        try:
            timing = make_timing(**attempt)
        except Exception as exc:
            print(f"  [{i+1}/{total}] skip {attempt} ({exc})", file=sys.stderr)
            continue
        label = _fmt_rate(attempt["nominal"])
        if attempt["data"] != attempt["nominal"]:
            label += f"/{_fmt_rate(attempt['data'])}"

        outcome = "timeout"
        saw_error = False
        try:
            with can.Bus(**config, timing=timing,
                         listen_only=True, error_frames=True) as bus:
                deadline = time.time() + timeout_s
                while True:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    msg = bus.recv(timeout=remaining)
                    if msg is None:
                        continue
                    if getattr(msg, "is_error_frame", False):
                        saw_error = True
                        continue
                    outcome = "valid"
                    break
        except Exception as exc:
            print(f"  [{i+1}/{total}] {label:>10} skipped ({exc})", file=sys.stderr)
            continue
        if outcome != "valid" and saw_error:
            outcome = "error"
        results.append(ProbeResult(attempt, outcome))
        print(f"  [{i+1}/{total}] {label:>10} -> {outcome}", file=sys.stderr)
        if outcome == "valid":
            return attempt, results
    return None, results


# ---------------------------------------------------------------------------
# Device / bus health status (read-only REST)
# ---------------------------------------------------------------------------

def _parse_channel(channel: str) -> tuple[str, int, str]:
    """Split a cansub channel 'host[:port]@ch' -> (host, port, ch).

    Mirrors python_can_cansub's parsing; default port 443.
    """
    address, ch = channel.split("@", 1)
    if ":" in address:
        host, port_str = address.rsplit(":", 1)
        port = int(port_str)
    else:
        host, port = address, 443
    return host, port, ch


def device_status(channel: str, timeout: float = 3.0) -> dict:
    """Read CAN controller status: GET https://{host}:{port}/api/can/{ch}.

    Returns the device JSON (state, frame_count, frame_rate, bus_load,
    rx_error_count, tx_error_count, bus_error_count) plus ok=True, or
    {ok: False, error: ...} on any failure. Read-only and never raises, so an
    init check can warn and continue.
    """
    try:
        import requests
        from python_can_cansub.cansub import CANSUB_ROOT_CERT
        host, port, ch = _parse_channel(channel)
        sess = requests.Session()
        sess.verify = str(CANSUB_ROOT_CERT)
        r = sess.get(f"https://{host}:{port}/api/can/{ch}", timeout=timeout)
        r.raise_for_status()
        d = dict(r.json())
        d["ok"] = True
        return d
    except Exception as exc:  # noqa: BLE001 - surfaced as a warning, never fatal
        return {"ok": False, "error": str(exc)}


_HEALTHY_STATES = {"error_active", "error_warning"}


def summarize_health(status: dict, profile: str | None = None) -> tuple[bool, list[str]]:
    """Turn a device_status dict into (ok, [human warnings]), profile-aware.

    profile: 'vehicle' (we are silent/listen_only) or 'bench' (we ACK). Used to
    tailor the hint for the no-ACK pathology.
    """
    if not status.get("ok"):
        return False, [f"could not read bus status: {status.get('error')}"]
    state = status.get("state", "unknown")
    fr = status.get("frame_rate", 0)
    rx = status.get("rx_error_count", 0)
    tx = status.get("tx_error_count", 0)
    be = status.get("bus_error_count", 0)
    warns: list[str] = []
    if state == "bus_off":
        warns.append("controller is BUS-OFF - it has dropped off the bus "
                     "(wiring, termination, or wrong bit-rate).")
    elif state == "error_passive":
        hint = ("on a single-node bench this is the classic NO-ACK symptom - use "
                "the bench profile so the CANsub ACKs."
                if profile == "vehicle"
                else "check wiring / termination / bit-rate.")
        warns.append(f"controller is ERROR-PASSIVE (many errors) - {hint}")
    elif state == "stopped":
        warns.append("controller is STOPPED (not started on the bus).")
    if fr == 0:
        warns.append("no frames flowing (frame_rate=0) - idle bus, wrong bit-rate, "
                     "or nothing is transmitting.")
    if be:
        warns.append(f"bus_error_count={be} (bus errors are occurring; e.g. a node "
                     "retransmitting because nothing ACKs it).")
    if tx and profile != "bench":
        warns.append(f"tx_error_count={tx} (the CANsub itself is hitting TX/ACK errors).")
    if rx and state not in _HEALTHY_STATES:
        warns.append(f"rx_error_count={rx}.")
    ok = state in _HEALTHY_STATES and fr > 0 and be == 0
    return ok, warns


# ---------------------------------------------------------------------------
# Trace loading (webCAN CSV)
# ---------------------------------------------------------------------------

WEBCAN_COLUMNS = [
    "TimestampEpoch", "BusChannel", "ID", "IDE", "DLC", "DataLength",
    "Dir", "EDL", "BRS", "ESI", "RTR", "DataBytes",
]


def load_trace(path: str, rx_only: bool = True) -> pd.DataFrame:
    """Load a webCAN-format CSV trace into a DataFrame.

    Columns: t (float epoch), ch (int), id (int), ext (bool), is_fd (bool),
    rtr (bool), data (bytes), length (int). Sorted by time.
    """
    df = pd.read_csv(path, sep=";", dtype=str).fillna("")
    if list(df.columns[:12]) != WEBCAN_COLUMNS:
        raise ValueError(f"Unexpected CSV header in {path}: {list(df.columns)}")

    out = pd.DataFrame()
    out["t"] = df["TimestampEpoch"].astype(float)
    out["ch"] = pd.to_numeric(df["BusChannel"], errors="coerce").astype("Int64")
    out["id"] = df["ID"].apply(lambda s: int(s, 16))
    out["ext"] = df["IDE"] == "1"
    out["is_fd"] = df["EDL"] == "1"
    out["rtr"] = df["RTR"] == "1"
    out["dir"] = pd.to_numeric(df["Dir"], errors="coerce").astype("Int64")
    out["data"] = df["DataBytes"].apply(bytes.fromhex)
    out["length"] = out["data"].apply(len)
    if rx_only:
        out = out[out["dir"] == 0]
    return out.sort_values("t").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Per-ID grouping + bitfield extraction
# ---------------------------------------------------------------------------

@dataclass
class IdGroup:
    can_id: int
    ext: bool
    t: np.ndarray            # float64 timestamps
    length: int              # modal payload length (bytes)
    le_int: np.ndarray       # object array of Python int, int.from_bytes(.,'little')
    be_int: np.ndarray       # object array of Python int, int.from_bytes(.,'big')
    n: int


def group_by_id(df: pd.DataFrame, max_len: int = 64) -> dict[int, IdGroup]:
    """Group frames by CAN id into Python-int payload integers for extraction.

    Only frames with the group's modal length and <= max_len bytes are kept.
    Payloads are packed as arbitrary-precision Python ints (object arrays) so this
    handles CAN FD payloads up to 64 bytes, not just classical 8.
    """
    groups: dict[int, IdGroup] = {}
    for can_id, g in df.groupby("id"):
        lengths = g["length"].to_numpy()
        modal = int(np.bincount(lengths).argmax())
        if modal == 0 or modal > max_len:
            continue
        g = g[g["length"] == modal]
        t = g["t"].to_numpy(dtype=np.float64)
        data = list(g["data"])
        le = np.array([int.from_bytes(d, "little") for d in data], dtype=object)
        be = np.array([int.from_bytes(d, "big") for d in data], dtype=object)
        groups[int(can_id)] = IdGroup(
            can_id=int(can_id), ext=bool(g["ext"].iloc[0]), t=t,
            length=modal, le_int=le, be_int=be, n=len(g),
        )
    return groups


def extract_le(le_int: np.ndarray, start: int, length: int) -> np.ndarray:
    """Little-endian (Intel) field as float64 raw, cantools start=start.

    le_int is an object array of Python ints, so start may exceed 63 (CAN FD).
    """
    mask = (1 << length) - 1
    raw = (le_int >> start) & mask
    return raw.astype(np.float64)


def extract_be(be_int: np.ndarray, payload_len: int, byte_off: int,
               width_bytes: int) -> np.ndarray:
    """Byte-aligned big-endian (Motorola) field as float64 raw."""
    shift = 8 * (payload_len - byte_off - width_bytes)
    mask = (1 << (8 * width_bytes)) - 1
    raw = (be_int >> shift) & mask
    return raw.astype(np.float64)


def apply_sign(raw: np.ndarray, length: int, signed: bool) -> np.ndarray:
    """Interpret raw as two's complement if signed (float-safe for length<=53)."""
    if not signed:
        return raw
    full = float(1 << length)
    half = float(1 << (length - 1))
    return np.where(raw >= half, raw - full, raw)


def extract_field(g: "IdGroup", order: str, signed: bool, byte: int | None = None,
                  width: int | None = None, start_bit: int | None = None,
                  length_bits: int | None = None) -> tuple[np.ndarray, int, int]:
    """Extract a field from a group → (raw float64 array, cantools_start, length_bits).

    Give either byte+width (byte-aligned, little or big) or start_bit+length_bits
    (Intel/little-only). Mirrors the cantools start-bit convention so the result
    maps straight into make_single_signal_db.
    """
    if start_bit is not None and length_bits is not None:
        if order != "little":
            raise ValueError("start-bit/length-bits is Intel(little)-only; "
                             "use byte/width for Motorola")
        length = length_bits
        raw = extract_le(g.le_int, start_bit, length)
        ct_start = start_bit
    elif byte is not None and width is not None:
        length = width * 8
        if order == "little":
            raw = extract_le(g.le_int, byte * 8, length)
            ct_start = byte * 8
        else:
            raw = extract_be(g.be_int, g.length, byte, width)
            ct_start = byte * 8 + 7
    else:
        raise ValueError("give either byte+width or start_bit+length_bits")
    return apply_sign(raw, length, signed), ct_start, length


# ---------------------------------------------------------------------------
# Resolution refinement (reference-free, from TRANSITION data)
# ---------------------------------------------------------------------------
# A field located from sparse STEADY HOLDS has a reliable MSB (the holds span the
# range) but can be UNDER-RESOLVED on the LSB side: at well-separated holds a coarse
# high-bit slice and the full-width field reproduce the same discrete levels equally
# well, so the identification's parsimony rule keeps the shorter field. The
# discriminating information lives in the TRANSITIONS between holds (recorded
# continuously, just unlabelled): the true-width field ramps smoothly through many
# fine values there, while a too-narrow slice staircases. This is reference-free -
# you don't need the true value to see a smooth ramp vs a staircase - so it recovers
# resolution that windows-only (holds) scoring cannot.

def _motion_mask(v: np.ndarray, t: np.ndarray, dilate_s: float) -> np.ndarray:
    """Frames where the signal is in TRANSITION: where v changes, dilated by
    +/- dilate_s so the whole ramp around each step is covered and steady holds are
    excluded. v is the located (reliable) field, which steps a few times per ramp."""
    v = np.asarray(v, np.float64)
    t = np.asarray(t, np.float64)
    mask = np.zeros(v.shape, bool)
    for i in np.flatnonzero(np.diff(v) != 0):
        lo = int(np.searchsorted(t, t[i] - dilate_s))
        hi = int(np.searchsorted(t, t[i] + dilate_s))
        mask[lo:hi] = True
    return mask


def _field_resolution(v: np.ndarray, motion: np.ndarray) -> int:
    """Number of DISTINCT values the field takes during motion. A well-resolved field
    visits many distinct values across the ramps (and also separates holds that a
    coarse slice merges); a too-coarse slice visits few. Scale-robust: it grows ~2x
    per real low bit and is UNCHANGED by a constant padding bit (x and 2*x have the
    same distinct count), so it stops growing at the lowest bit that carries signal."""
    sub = v[motion]
    if sub.size < 2:
        return 1
    return int(np.unique(sub).size)


def _field_smoothness(v: np.ndarray, motion: np.ndarray) -> float:
    """1 - sign-change rate of the field's nonzero steps during motion. A real ramp
    keeps a consistent direction within each transition (smooth, ~1.0); a noise or
    neighbour low bit flips direction frame-to-frame (~0.5). This is the gate that
    stops the LSB from growing into bits that are not part of the signal."""
    d = np.diff(v)
    d = d[motion[1:] & (d != 0)]
    if len(d) < 3:
        return 1.0
    s = np.sign(d)
    return float(1.0 - np.sum(s[1:] != s[:-1]) / (len(s) - 1))


def bit_flip_rates(byte_arr: np.ndarray, modal: int):
    """Per-bit flip-rate + changing mask in LSB-FIRST global index.

    Global bit k = byte*8 + bit_in_byte with bit_in_byte 0 = the byte's LSB - the
    SAME convention as extract_le / cantools little-endian start bits (so a survey
    bit index drops straight into bitsearch/build_dbc). flip-rate[k] = fraction of
    consecutive frames where bit k changes. This is the single source of truth for
    per-bit activity; survey.py and the cascade resolution refinement both use it.
    """
    nbits = modal * 8
    rate = np.zeros(nbits)
    changing = np.zeros(nbits, dtype=bool)
    if byte_arr.shape[0] < 1:
        return rate, changing
    for b in range(modal):
        col = byte_arr[:, b].astype(np.int64)
        for j in range(8):
            bit = (col >> j) & 1
            k = b * 8 + j
            changing[k] = bool(bit.min() != bit.max())
            if bit.size > 1:
                rate[k] = float(np.mean(np.abs(np.diff(bit)) != 0))
    return rate, changing


def _bit_flip_rates_le(le_int: np.ndarray, nbits: int) -> np.ndarray:
    """Per-bit flip-rate (LSB-first) computed from a group's le_int integers.

    Builds the byte matrix the shared `bit_flip_rates` core expects, so a CAN FD
    payload (le_int wider than 64 bits) is handled too. Returns only `rate` (the
    caller derives `changing` from rate > 0)."""
    ints = [int(v) for v in le_int]
    n = len(ints)
    if n < 1:
        return np.zeros(nbits)
    nbytes = (nbits + 7) // 8
    byte_arr = np.empty((n, nbytes), dtype=np.int64)
    for bi in range(nbytes):
        sh = 8 * bi
        byte_arr[:, bi] = [(v >> sh) & 0xFF for v in ints]
    rate, _ = bit_flip_rates(byte_arr, nbytes)
    return rate[:nbits]


def _cascade_lsb(rate: np.ndarray, n_frames: int, msb: int, *, jump: float = 4.0,
                 drop: float = 0.5, min_flips: int = 30,
                 tiny: float = 1e-6) -> tuple[int, str]:
    """Reference-free LSB boundary of a little-endian field from the flip-rate cascade.

    Within ONE contiguous little-endian integer field the per-bit flip-rate roughly
    DOUBLES each step toward the LSB (the low bits toggle most). So, starting from the
    reliable MSB, walk down across the active run and keep going while each lower bit
    continues that descent. Stop and report why:
      - "constant": the next bit never toggles (rate ~ 0) -> we are below the field's
        exercised LSB (its true low boundary, or padding/sub-resolution below it);
      - "jump": the next bit's flip-rate leaps far above the descent (>= `jump` x the
        bit just above) -> a SEPARATE, faster field's LSB intruding from below (a fast
        counter, or the low field of an over-wide pair) - do NOT swallow it;
      - "drop": the next bit's flip-rate FALLS sharply (< `drop` x the bit above) ->
        we have left the field into the slow high bits of a separate LOWER field;
      - "floor": reached bit 0 still in the cascade (low confidence; no clean boundary).

    Crucially this EXPECTS fast, dithering LSBs - the opposite of the smoothness gate,
    which a noisy real-time analog signal's genuine low bits would trip. The jump/drop
    tests are skipped while the bit above has too few flips (`min_flips`) for its rate to
    be a meaningful denominator, so statistical noise at the rarely-toggling high bits
    does not fake a boundary."""
    lsb, reason = msb, "floor"
    for k in range(msb - 1, -1, -1):
        if rate[k] <= tiny:
            reason = "constant"
            break
        above = rate[k + 1]
        if above * n_frames >= min_flips:
            if rate[k] > jump * max(above, tiny):
                reason = "jump"
                break
            if rate[k] < drop * above:
                reason = "drop"
                break
        lsb = k
    return lsb, reason


def refine_field_resolution(le_int: np.ndarray, t: np.ndarray, order: str,
                            start_bit: int, length: int, signed: bool, *,
                            dilate_s: float = 0.5, smooth_floor: float = 0.7,
                            res_gain: float = 0.3, rate: np.ndarray | None = None,
                            min_frames: int = 40) -> tuple[int, int]:
    """Grow an under-read little-endian field LSB-ward to its true resolution.

    Two complementary, reference-free mechanisms widen the LSB (the MSB, reliable from
    full-range holds, is kept):

    (1) SMOOTHNESS/distinct-count growth (the clean-ramp regime, e.g. an offline
        machine reference): extend the LSB downward one bit at a time, adopting a lower
        bit only when it makes the decode visibly FINER during motion (distinct-value
        count rises by > res_gain) while staying SMOOTH. Stops as soon as a lower bit
        injects non-monotonic jitter. Correct when the low bits ramp cleanly.

    (2) FLIP-RATE CASCADE growth (the noisy live-stream regime): a noisy real-time
        analog signal's genuine LSBs DITHER (flip direction frame-to-frame), which the
        smoothness gate in (1) wrongly rejects as noise - so (1) alone under-reads the
        width. The cascade instead EXPECTS fast LSBs: it grows across the contiguous
        active run, stopping only at a constant bit or a flip-rate JUMP (a separate,
        faster field below). See `_cascade_lsb`. Adopted only when it stops on such a
        confident boundary, and only when it WIDENS the result.

    The two never fight: (1) only ever widens by smooth resolution gain, (2) only ever
    widens further across a confident boundary; the wider (lower) LSB wins.

    Returns (start_bit, length). Little-endian (Intel) only - that is where the
    parsimony-picks-narrow failure occurs; other orders are returned unchanged. Pass a
    precomputed per-bit `rate` (LSB-first) to avoid recomputing it.
    """
    if order != "little" or start_bit <= 0:
        return start_bit, length
    msb = start_bit + length - 1
    n_frames = len(le_int)

    def field(lsb: int) -> np.ndarray:
        n = msb - lsb + 1
        return apply_sign(extract_le(le_int, lsb, n), n, signed).astype(np.float64)

    # (1) smoothness/distinct-count growth - needs captured transitions (ramps)
    smooth_lsb = start_bit
    base = field(start_bit)
    motion = _motion_mask(base, t, dilate_s)
    if int(motion.sum()) >= 30:
        best_res = _field_resolution(base, motion)
        for lsb in range(start_bit - 1, -1, -1):
            v = field(lsb)
            if _field_smoothness(v, motion) < smooth_floor:
                break                         # added bit is noise / a neighbour -> stop
            res = _field_resolution(v, motion)
            if res > best_res * (1.0 + res_gain):
                smooth_lsb, best_res = lsb, res  # real added resolution -> grow LSB-ward

    # (2) flip-rate cascade widener (dither-tolerant; ramps not required, just movement).
    # A CONFIDENT structural boundary (a constant bit, or a jump/drop into a separate
    # field) is AUTHORITATIVE: it both widens past a smooth result that stalled on a
    # noisy signal's dithering LSBs AND reins in a smooth result that over-grew into a
    # slowly-toggling neighbour (e.g. a rolling counter's high bits). It never narrows
    # below the trusted input field. A "floor" (no boundary - one cascade all the way to
    # bit 0, i.e. a fully-exercised field) defers to the smooth result.
    final_lsb = smooth_lsb
    if n_frames >= min_frames:
        if rate is None:
            rate = _bit_flip_rates_le(le_int, msb + 1)
        cas_lsb, reason = _cascade_lsb(rate, n_frames, msb)
        if reason in ("constant", "jump", "drop"):
            final_lsb = min(cas_lsb, start_bit)

    return final_lsb, msb - final_lsb + 1


def snap_to_boundary(start_bit: int, length: int, changing: np.ndarray, *,
                     signed: bool = False, occupied=None,
                     widths=(8, 16, 32, 64)) -> dict | None:
    """Propose a byte/word-aligned field that ENCLOSES an exercised little-endian field
    by extending only across CONSTANT bits (never across another active bit).

    The flip-rate cascade pins the EXERCISED extent (the bits that actually moved). A
    real OEM field is usually byte/word-aligned and wider: bits below the exercised LSB
    are often sub-resolution (the source quantizes, so they stay 0), and bits above the
    exercised MSB are simply an unexercised part of the range. Snapping outward across
    those constant bits to the smallest standard width that contains the exercised field
    reproduces the canonical definition WITHOUT changing the physical decode (constant
    filler bits contribute a fixed value, folded into the offset). It DOES change the
    reported SCALE by 2^(start-astart) when the LSB moves down - which is why the caller
    treats this as advisory / opt-in (`--byte-align`), never a silent rewrite.

    Returns None when no clean aligned field fits or the field is already aligned; else a
    dict with the aligned geometry, the scale factor, which side moved, and the constant
    filler bits (below / above) for reporting and the cascade plot. MSB extension is
    suppressed for signed fields (it would move the sign bit and reinterpret two's
    complement)."""
    n = len(changing)
    ex_msb = start_bit + length - 1
    if ex_msb >= n:
        return None
    occ = set(occupied or [])
    chosen = None
    for W in sorted(widths):
        if W < length:
            continue
        cands = []
        for A in range(0, start_bit + 1, 8):
            B = A + W - 1
            if B < ex_msb or B >= n:
                continue
            if signed and B != ex_msb:
                continue                       # never move the sign bit (MSB)
            filler = [b for b in range(A, B + 1) if not (start_bit <= b <= ex_msb)]
            if any(changing[b] or b in occ for b in filler):
                continue                       # would swallow another active field
            cands.append((A, B, filler))
        if cands:
            chosen = max(cands, key=lambda c: c[0])   # largest A = least LSB movement
            break
    if chosen is None:
        return None
    A, B, filler = chosen
    alen = B - A + 1
    if A == start_bit and alen == length:
        return None                            # already byte-aligned standard width
    return {
        "exercised": (start_bit, length),
        "aligned": (A, alen),
        "scale_factor": 1 << (start_bit - A),  # raw_aligned = raw_exercised << this shift
        "lsb_moved": A < start_bit,
        "msb_moved": B > ex_msb,
        "constant_below": [b for b in filler if b < start_bit],
        "constant_above": [b for b in filler if b > ex_msb],
    }


# ---------------------------------------------------------------------------
# cantools single-signal DBC mapping
# ---------------------------------------------------------------------------

def cantools_start(order: str, byte_off: int, length: int, payload_len: int) -> int:
    """Map a field to a cantools `start` bit.

    little: start = global LSB bit index = byte_off*8 (for byte-aligned) — here
            we pass the LSB bit index directly via `byte_off` reinterpreted as a
            bit offset is NOT done; callers pass bit start for LE via build path.
    big:    start = byte_off*8 + 7 (MSB of first byte, sawtooth numbering).
    """
    if order == "little":
        return byte_off  # caller passes the LSB *bit* index for little-endian
    return byte_off * 8 + 7


def theoretical_range(length: int, signed: bool, scale: float = 1.0,
                      offset: float = 0.0) -> tuple[float, float]:
    """Full representable physical range of a field -> (minimum, maximum).

    The DBC min/max should describe what the encoding CAN represent (every code
    the bits can hold), NOT what a particular log happened to contain - an empty
    car parked at 0 doesn't make 0 the signal's maximum. Derived from the raw code
    range (unsigned 0..2^n-1, or signed -2^(n-1)..2^(n-1)-1) mapped through
    offset + scale*raw, ordered (a negative scale flips the endpoints).
    """
    if signed:
        rmin, rmax = -(1 << (length - 1)), (1 << (length - 1)) - 1
    else:
        rmin, rmax = 0, (1 << length) - 1
    a, b = offset + scale * rmin, offset + scale * rmax
    return (float(min(a, b)), float(max(a, b)))


def make_single_signal_db(
    name: str, can_id: int, ext: bool, payload_len: int,
    start: int, length: int, order: str, signed: bool,
    scale: float = 1.0, offset: float = 0.0,
    minimum=None, maximum=None, message_name: str | None = None,
) -> cantools.database.Database:
    """Build an in-memory single-signal cantools Database.

    `start` is the cantools start bit (LSB index for little, MSB index for big).
    `minimum`/`maximum` default to the field's THEORETICAL representable range
    (`theoretical_range`), not an observed data range.
    """
    if minimum is None or maximum is None:
        tmin, tmax = theoretical_range(length, signed, scale, offset)
        minimum = tmin if minimum is None else minimum
        maximum = tmax if maximum is None else maximum
    byte_order = "little_endian" if order == "little" else "big_endian"
    conversion = cantools.database.conversion.BaseConversion.factory(
        scale=scale, offset=offset)
    sig = cantools.database.can.Signal(
        name=name, start=start, length=length, byte_order=byte_order,
        is_signed=signed, conversion=conversion,
        minimum=minimum, maximum=maximum,
    )
    msg = cantools.database.can.Message(
        frame_id=can_id, name=message_name or f"MSG_0x{can_id:X}",
        length=payload_len, is_extended_frame=ext, is_fd=payload_len > 8,
        signals=[sig],
    )
    return cantools.database.Database(messages=[msg])


# ---------------------------------------------------------------------------
# Sidecar reference loading
# ---------------------------------------------------------------------------

SIDECAR_COLUMNS = ["epoch", "kind", "label", "value"]


def load_sidecar(path: str) -> pd.DataFrame:
    """Load a sidecar annotation CSV (epoch;kind;label;value)."""
    df = pd.read_csv(path, sep=";", dtype=str).fillna("")
    df["epoch"] = df["epoch"].astype(float)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.sort_values("epoch").reset_index(drop=True)


def continuous_reference(sidecar: pd.DataFrame):
    """Return (times, values) step series of slider/value samples."""
    rows = sidecar[sidecar["kind"] == "value"].dropna(subset=["value"])
    return rows["epoch"].to_numpy(np.float64), rows["value"].to_numpy(np.float64)


def discrete_events(sidecar: pd.DataFrame) -> np.ndarray:
    """Return event marker timestamps."""
    rows = sidecar[sidecar["kind"] == "event"]
    return rows["epoch"].to_numpy(np.float64)


def anchor_reference(sidecar: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Return (times, values) of kind=="anchor" known-value tags.

    Anchors are discrete (timestamp, known physical value) pairs logged when a
    human parks a signal at a known value and tags it; consumed by calibrate.py.
    """
    rows = sidecar[sidecar["kind"] == "anchor"].dropna(subset=["value"])
    return rows["epoch"].to_numpy(np.float64), rows["value"].to_numpy(np.float64)


def sample_hold(times: np.ndarray, values: np.ndarray,
                query_t: np.ndarray) -> np.ndarray:
    """Forward (sample-and-hold) interpolation of a step series at query_t.

    The most recent value at or before each query time (NaN before the first).
    """
    idx = np.searchsorted(times, query_t, side="right") - 1
    out = np.full(query_t.shape, np.nan, dtype=np.float64)
    valid = idx >= 0
    out[valid] = values[idx[valid]]
    return out


def windowed_sample(anchor_t: np.ndarray, anchor_v: np.ndarray,
                    query_t: np.ndarray, window: float, guard: float = 0.0,
                    lag: float = 0.0) -> np.ndarray:
    """Per-sample reference using ONLY the fixed window after each anchor tag.

    The windows-only holds model: each kind=="anchor" tag opens a sampling window
    [t+guard, t+window]; the reference is that anchor's known value INSIDE its
    window and NaN everywhere else. Because the downstream scoring masks on
    isfinite, this restricts every scored sample to deliberately-held steady data
    and excludes the transitions between holds by construction. `lag` shifts the
    windows in time to absorb human reaction delay (≈0 here, since the operator
    settles before tagging). Overlapping windows: the later tag wins.
    """
    q = np.asarray(query_t, dtype=np.float64)
    out = np.full(q.shape, np.nan, dtype=np.float64)
    for ti, vi in zip(np.asarray(anchor_t, np.float64), np.asarray(anchor_v, np.float64)):
        out[(q >= ti + guard + lag) & (q <= ti + window + lag)] = vi
    return out


def make_reference_sampler(sidecar: pd.DataFrame, window: float | None = None,
                           guard: float = 0.0):
    """Build a reference sampler f(query_t, lag=0.0) -> per-sample reference array.

    window=None  -> CONTINUOUS reference: the kind=="value" step series, forward
                    sample-and-hold (slider runs, or legacy continuous holds).
    window=float -> WINDOWS-ONLY reference: the fixed window after each kind=="anchor"
                    tag (see windowed_sample); transitions are NaN, so scoring/fit use
                    only the deliberately-held steady data. Callers still decode the
                    FULL signal for plotting - only the *reference* is windowed.

    The returned callable carries `.windowed` (bool), and for the windowed case
    `.spans` = list of (t_start, t_end, value) for shading the sampled windows.
    """
    if window is None:
        ref_t, ref_v = continuous_reference(sidecar)

        def f(query_t, lag=0.0):
            return sample_hold(ref_t + lag, ref_v,
                               np.asarray(query_t, dtype=np.float64))
        f.windowed = False
        f.spans = None
        return f
    at, av = anchor_reference(sidecar)

    def f(query_t, lag=0.0):
        return windowed_sample(at, av, query_t, window, guard, lag)
    f.windowed = True
    f.spans = [(float(ti), float(ti + window), float(vi)) for ti, vi in zip(at, av)]
    return f


# ---------------------------------------------------------------------------
# Robust correlation scoring (shared by correlate / bitsearch / verify)
# ---------------------------------------------------------------------------

GRID_HZ = 20.0          # resample grid for continuous correlation (Hz)
LAG_WINDOW_S = 2.0      # +/- search for human reaction delay (s)
LAG_STEPS = 21          # lag-search grid resolution
N_WINDOWS = 6           # windows for outlier-robust median scoring


def _windowed_spearman_signed(a: np.ndarray, b: np.ndarray,
                              n_windows: int) -> tuple[float, int]:
    """Median |Spearman| over contiguous windows, PLUS the correlation sign.

    Returns (magnitude, sign) where magnitude is the outlier-robust median of
    |rho| over `n_windows` chunks (the quantity used for ranking), and sign is
    +1 if the field tracks the reference, -1 if it is anti-correlated (i.e. a
    negative scale), or 0 if undetermined. Splitting the older `abs()`-only
    score into (magnitude, sign) lets the downstream fit *expect* a negative
    scale instead of treating it as a silent red flag.
    """
    valid = np.isfinite(a) & np.isfinite(b)
    a, b = a[valid], b[valid]
    if len(a) < 10:
        return 0.0, 0
    signed = []
    for chunk_a, chunk_b in zip(np.array_split(a, n_windows),
                                np.array_split(b, n_windows)):
        if len(chunk_a) < 5 or np.ptp(chunk_a) == 0 or np.ptp(chunk_b) == 0:
            continue
        rho = stats.spearmanr(chunk_a, chunk_b).statistic
        if np.isfinite(rho):
            signed.append(float(rho))
    if not signed:
        return 0.0, 0
    magnitude = float(np.median(np.abs(signed)))
    sign = 1 if float(np.median(signed)) >= 0 else -1
    return magnitude, sign


def _windowed_spearman(a: np.ndarray, b: np.ndarray, n_windows: int) -> float:
    """Median |Spearman| over contiguous windows (robust to bad segments)."""
    return _windowed_spearman_signed(a, b, n_windows)[0]


def plausibility(raw_unsigned: np.ndarray, length: int,
                 period_s: float | None = None) -> dict:
    """Reference-FREE physical-plausibility score of a decoded raw series.

    A correctly-located continuous field decodes to a smooth, wrap-free integer
    series sampled at the frame rate. A mis-located / wrong-endian / over-wide /
    sub-slice field instead shows two tells: (a) MODULAR WRAPS - per-frame deltas
    near +/-2**length (the read straddles a real field boundary, or a value was
    read with the wrong byte significance); and (b) WHITE-NOISE JUMPS - large
    deltas relative to the field's own range, every frame. Both metrics are
    scale-free (relative to the field's own modulus and observed range), so a
    sensor using 5% of its range scores like one using 100%.

    raw_unsigned : UNSIGNED integer field values in frame order (NOT resampled -
                   we want the true per-frame deltas).
    length       : field length in bits (defines the modulus 2**length).
    period_s     : modal frame period (s). Reserved for a future absolute slew
                   budget; thresholds today are purely relative, so it is unused.

    Returns {wrap_rate, jump_rate, ac1, score}, score in [0,1] (higher = more
    physically plausible).
    """
    raw = np.asarray(raw_unsigned, dtype=np.float64)
    n = raw.size
    rng = float(np.ptp(raw)) if n else 0.0
    if n < 8 or rng == 0.0:
        return {"wrap_rate": 0.0, "jump_rate": 0.0, "ac1": 0.0, "score": 0.0}
    mod = float(1 << int(length))
    d = np.diff(raw)
    # (a) modular-wrap rate: deltas whose magnitude is a large fraction of the
    #     full field modulus (real physical quantities don't wrap their field).
    wrap_rate = float(np.mean(np.abs(d) > 0.5 * mod))
    # (b) per-frame jump rate: deltas exceeding a generous fraction of the field's
    #     OWN observed range -> invariant to scale/length and to sub-range use.
    jump_rate = float(np.mean(np.abs(d) > 0.25 * rng))
    # (c) smoothness: lag-1 autocorrelation (a real signal is autocorrelated
    #     frame-to-frame; scrambled bits are near-white).
    s = raw - raw.mean()
    denom = float(np.dot(s, s)) or 1.0
    ac1 = float(np.dot(s[:-1], s[1:]) / denom)
    score = max(0.0, ac1) * (1.0 - wrap_rate) * (1.0 - 0.5 * jump_rate)
    return {"wrap_rate": round(wrap_rate, 4), "jump_rate": round(jump_rate, 4),
            "ac1": round(ac1, 4), "score": round(float(np.clip(score, 0.0, 1.0)), 4)}


# "Nice" scales an OEM tends to pick: simple decimals m*10**k and binary
# fractions 2**-j, across a wide exponent range (covers GPS 1e-7 .. counts 1).
def scale_plausibility(scale: float, tol: float = 0.02) -> dict:
    """How close is |scale| to a 'nice' OEM value? -> {nice, nearest, rel_err}.

    Nice = m*10**k (m in {1,2,2.5,5}, k in [-12,6]) OR 2**-j (j in [0,23]). A
    fitted scale that is NOT near a nice value (e.g. -1/2593 = -3.856e-4) usually
    means the field geometry is wrong (sub-slice / wrong endianness). Legitimate
    high-precision scales like 1e-6 (lat/long), 1e-7, 0.001 (speed), 0.125 are
    nice -> not flagged.

    ONE-WAY HINT: non-nice => probably wrong, but nice does NOT prove the geometry
    is right. An OVER-WIDE read that appends whole bytes scales true/256**k; for a
    binary-fraction true scale (2**-j) that stays a binary fraction (2**-(j+8k)),
    so it reads as nice. That failure mode is handled by bitsearch's parsimony
    rule (`_suppress_overlaps`), not by this check.
    """
    a = abs(float(scale))
    if a == 0.0 or not np.isfinite(a):
        return {"nice": False, "nearest": 0.0, "rel_err": float("inf")}
    cands = [m * 10.0 ** k for k in range(-12, 7) for m in (1.0, 2.0, 2.5, 5.0)]
    cands += [2.0 ** -j for j in range(0, 24)]
    cands = np.array(cands, dtype=np.float64)
    nearest = float(cands[np.argmin(np.abs(np.log(cands) - np.log(a)))])
    rel_err = abs(a - nearest) / nearest
    return {"nice": bool(rel_err <= tol), "nearest": nearest,
            "rel_err": round(float(rel_err), 4)}


# ---------------------------------------------------------------------------
# Extreme-outlier (saturation / "value unavailable") detection
# ---------------------------------------------------------------------------

def to_unsigned(raw: np.ndarray, length: int) -> np.ndarray:
    """Unsigned bit-pattern value of a (possibly sign-applied) field."""
    return np.mod(np.asarray(raw, dtype=np.float64), float(1 << int(length)))


def _is_all_ones(v: float) -> bool:
    """True if the integer v is a run of set bits (0b1, 0b11, 0b111, ... = 2^k-1)."""
    iv = int(round(v))
    return iv > 0 and (iv & (iv + 1)) == 0


def _robust_band(x: np.ndarray) -> tuple[float, float, float]:
    """Robust centre + scale of a value array -> (median, MAD, scale).

    Uses the 1.4826-scaled median-absolute-deviation, which is unaffected by up to
    ~50% contamination - so a 4-10% sentinel cluster does NOT move the band the way
    a p2/p98 band did (the 98th percentile can sit *on* the cluster). Falls back to
    a (scaled) IQR when the MAD is zero (a tight integer bulk), then floors at 1.0
    so the z-space is always well defined.
    """
    med = float(np.median(x))
    mad = 1.4826 * float(np.median(np.abs(x - med)))
    if mad <= 0.0:
        q1, q3 = (float(v) for v in np.percentile(x, [25, 75]))
        mad = (q3 - q1) / 1.349
    scale = max(mad, 1.0)
    return med, mad, scale


def _structural_anchors(length: int) -> list[float]:
    """Unsigned codes that act as 'signal invalid' sentinels across encodings:
    0, the top code (all-ones of the field), the most-negative code (signed
    sentinel), and every shorter all-ones run 2^k-1 (e.g. a 14-bit 0x3FFF read
    inside a 16-bit field)."""
    L = int(length)
    anchors = {0.0, float((1 << L) - 1), float(1 << (L - 1))}
    anchors.update(float((1 << k) - 1) for k in range(1, L + 1))
    return sorted(anchors)


def detect_extreme_outliers(raw: np.ndarray, length: int,
                            gap_factor: float = 4.0, max_frac: float = 0.02,
                            min_n: int = 20, t: np.ndarray | None = None,
                            k_band: float = 6.0, gap_mad: float = 4.0,
                            teleport_p: float = 0.999, teleport_factor: float = 3.0,
                            sanity_frac: float = 0.45) -> dict | None:
    """Flag sentinel / out-of-band samples in a decoded field - AGNOSTICALLY.

    A "value unavailable / signal invalid" sentinel reads FAR outside the real data
    band (a 2-byte speed at 0.1 reading 0xFFFF -> 6553.5; an engine-off RPM reading
    0x3FFF -> 16383). The detector must NOT assume the sentinel is all-ones, a
    single value, a small fraction, or on the high side - earlier code keyed on a
    fixed all-ones code and a 2% frequency cap and so missed a 4.4%, multi-valued,
    14-bit-all-ones (0x3FFF/0x4000/0x4041) engine-off sentinel. It rests on four
    scale-free structural pillars; the bit pattern is only a CONFIDENCE hint:

      1. ROBUST BAND - median +/- k_band*MAD (see _robust_band). MAD survives a
         4-10% cluster, so the band stays on the bulk.
      2. CLEAR GAP - the out-of-band cluster must be separated from the bulk by an
         EMPTY gap of >= gap_mad MAD-units, tested per side (high AND low). A
         gapless full-range continuous signal qualifies on NEITHER side -> nothing
         flagged. This is the primary false-positive guard.
      3. TELEPORT - each contiguous out-of-band run (an "episode") must be entered
         or left by a per-frame step far larger (teleport_factor x) than the robust
         in-band slew budget (the teleport_p percentile of in-band |delta|). A
         sentinel teleports in one frame; a real signal ramps. Uses t for a true
         per-second slew when strictly increasing, else raw per-frame deltas.
      4. EPISODES + SANITY CEILING - count contiguous runs, not raw frames (a long
         engine-off stretch is a couple of episodes). Bail only above sanity_frac
         (band meaningless). max_frac/gap_factor are accepted for back-compat and
         now only shape confidence; they no longer GATE detection.

    raw    : field values AS DECODED (two's-complement already applied if signed),
             in FRAME ORDER (per-frame deltas drive the teleport test).
    length : field length in bits (defines the structural anchors).
    t      : optional per-frame timestamps (for a true slew rate).

    Returns a dict (superset of the legacy keys: mask, count, frac, values,
    u_values, looks_maxed, topcode, hi_thr, lo_thr; plus med, mad, gap_mad_hi,
    gap_mad_lo, episodes, confirmed_episodes, slew_budget, cluster_spread,
    near_structural, bottomcode, kind, confidence) or None when no side shows a
    clear, separated cluster.
    """
    raw = np.asarray(raw, dtype=np.float64)
    finite = np.isfinite(raw)
    n = int(finite.sum())
    if n < min_n:
        return None
    x = raw[finite]
    med, mad, scale = _robust_band(x)
    z = (raw - med) / scale                       # nan where raw is non-finite
    cand = finite & (np.abs(z) > k_band)
    if int(cand.sum()) == 0:
        return None
    if cand.sum() / n > sanity_frac:              # band estimate meaningless -> bail
        return None

    # --- pillar 2: clear empty gap, per side -------------------------------------
    def _side_gap(side_mask: np.ndarray, hi: bool) -> float:
        if not side_mask.any():
            return 0.0
        zc = z[side_mask]
        edge = float(zc.min()) if hi else float(zc.max())
        below = z[finite & ((z < edge) if hi else (z > edge))]
        if below.size == 0:
            return 0.0
        nearest = float(below.max()) if hi else float(below.min())
        return abs(edge - nearest)

    mask_hi = cand & (z > 0)
    mask_lo = cand & (z < 0)
    gap_hi = _side_gap(mask_hi, hi=True)
    gap_lo = _side_gap(mask_lo, hi=False)
    hi_ok = mask_hi.any() and gap_hi >= gap_mad
    lo_ok = mask_lo.any() and gap_lo >= gap_mad
    mask = np.zeros_like(finite)
    if hi_ok:
        mask |= mask_hi
    if lo_ok:
        mask |= mask_lo
    cnt = int(mask.sum())
    if cnt == 0:
        return None

    cluster_spread = float(np.ptp(z[mask])) if cnt > 1 else 0.0

    # --- pillar 3: teleport confirmation, per episode ----------------------------
    use_rate = t is not None and np.all(np.diff(np.asarray(t, float)) > 0)
    d = np.abs(np.diff(raw))
    if use_rate:
        d = d / np.diff(np.asarray(t, dtype=np.float64))
    in_band = finite & ~mask
    pair_ok = in_band[:-1] & in_band[1:]
    d_in = d[pair_ok & np.isfinite(d)]
    if d_in.size >= 10:
        budget = float(np.percentile(d_in, 100.0 * teleport_p))
    elif d_in.size:
        budget = float(d_in.max())
    else:
        budget = scale
    budget = max(budget, 1e-9)

    episodes = confirmed = 0
    i = 0
    while i < len(mask):
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(mask) and mask[j + 1]:
            j += 1
        episodes += 1
        edges = []
        if i - 1 >= 0 and in_band[i - 1] and np.isfinite(d[i - 1]):
            edges.append(d[i - 1])                # entry boundary i-1 -> i
        if j + 1 < len(mask) and in_band[j + 1] and np.isfinite(d[j]):
            edges.append(d[j])                    # exit boundary j -> j+1
        if edges and max(edges) > teleport_factor * budget:
            confirmed += 1
        i = j + 1

    # --- structural-code confidence hint (NOT a gate) ----------------------------
    u = to_unsigned(raw[mask], length)
    uvals = sorted({float(v) for v in u})
    topcode = float((1 << int(length)) - 1)
    bottomcode = float(1 << (int(length) - 1))
    anchors = _structural_anchors(length)
    near_structural = any(min(abs(v - a) for a in anchors) <= 2.0 for v in uvals)
    looks_maxed = all(_is_all_ones(v) or v == topcode for v in uvals)

    # --- confidence + kind -------------------------------------------------------
    if episodes and confirmed == episodes:
        confidence = "high"
    elif confirmed >= 1:
        confidence = "medium"
    else:
        confidence = "low"
    order = ["low", "medium", "high"]
    if cluster_spread > gap_mad:                  # cluster looks like a 2nd distribution
        confidence = order[max(0, order.index(confidence) - 1)]
    if n < 2 * min_n:                             # sparse -> don't over-trust
        confidence = order[min(order.index(confidence), 1)]
    if confirmed >= 1:
        kind = "sentinel" if near_structural else "outlier"
    else:
        kind = "suspect"

    return {"mask": mask, "count": cnt, "frac": cnt / n,
            "values": sorted({float(v) for v in raw[mask]}), "u_values": uvals,
            "hi_thr": med + k_band * scale, "lo_thr": med - k_band * scale,
            "looks_maxed": bool(looks_maxed), "topcode": topcode,
            "bottomcode": bottomcode, "med": med, "mad": mad,
            "gap_mad_hi": gap_hi if hi_ok else 0.0,
            "gap_mad_lo": gap_lo if lo_ok else 0.0,
            "episodes": episodes, "confirmed_episodes": confirmed,
            "slew_budget": budget, "cluster_spread": cluster_spread,
            "near_structural": bool(near_structural),
            "kind": kind, "confidence": confidence}


def describe_extreme_outliers(info: dict, length: int,
                              scale: float | None = None,
                              offset: float = 0.0, unit: str = "") -> str:
    """One-line human summary of a detect_extreme_outliers() result."""
    u = info["u_values"]
    hexs = ", ".join(f"0x{int(v):0{(length + 3) // 4}X}" for v in u)
    kind = info.get("kind")
    if kind is None:        # defensive: legacy dict without the new fields
        kind = "all-ones / maxed-out sentinel" if info.get("looks_maxed") else "out-of-band"
    conf = info.get("confidence", "?")
    eps = info.get("episodes")
    gap = max(info.get("gap_mad_hi", 0.0), info.get("gap_mad_lo", 0.0))
    phys = ""
    if scale is not None and u:
        decoded = [offset + scale * v for v in u]
        phys = " -> decodes " + ", ".join(f"{d:.4g}{unit}" for d in decoded)
    ep = f" in {eps} episode(s)" if eps else ""
    return (f"{info['count']} frame(s){ep} ({100 * info['frac']:.2f}%) at raw {hexs} "
            f"[{kind}, {conf} confidence]{phys}; separated from the data band by a "
            f"{gap:.0f}*MAD gap.")


def auto_mask_outliers(raw: np.ndarray, length: int, t: np.ndarray | None = None,
                       *, require: str = "high",
                       max_mask_frac: float = 0.15) -> tuple[np.ndarray, dict | None]:
    """Mask sentinels OUT OF an identification series before scoring.

    Runs detect_extreme_outliers; if the detection meets `require` confidence AND
    masks no more than `max_mask_frac` of the finite samples, returns (copy of raw
    with those frames set to NaN, info). Otherwise returns (raw unchanged, None).
    Downstream scorers (_fit_line, plausibility on the finite subset, windowed
    Spearman) all isfinite-filter, so the masked frames simply drop out.

    The high-confidence gate + masked-fraction cap are what stop a WRONG field from
    being rescued: a mis-located field shows diffuse scatter (no clean gap, no
    isolated teleport) and never reaches high confidence, so nothing is masked.
    """
    info = detect_extreme_outliers(raw, length, t=t)
    if not info:
        return raw, None
    order = {"low": 0, "medium": 1, "high": 2}
    if order.get(info["confidence"], 0) < order.get(require, 2):
        return raw, None
    if info["frac"] > max_mask_frac:
        return raw, None
    out = np.array(raw, dtype=np.float64)
    out[info["mask"]] = np.nan
    return out, info


def residual_summary(decoded: np.ndarray, ref: np.ndarray) -> dict | None:
    """Shape-residual 5-number summary of a decoded series vs a reference.

    Affine-refits decoded->ref first (so an un-calibrated DBC or a constant lag
    offset is NOT penalised - we measure SHAPE agreement), then summarises the
    absolute residuals normalised by the reference range. A correct field's shape
    affine-maps onto the reference (small residuals); a wrong/scrambled field that
    only matches at a few anchor points does NOT (large residuals over the rest of
    the range - the "3-collinear-anchors" trap). Returns
    {n, median, p90, max, nrmse, p90_frac, range} or None if too thin.
    """
    decoded = np.asarray(decoded, dtype=np.float64)
    ref = np.asarray(ref, dtype=np.float64)
    valid = np.isfinite(decoded) & np.isfinite(ref)
    decoded, ref = decoded[valid], ref[valid]
    if len(decoded) < 5 or np.ptp(decoded) == 0:
        return None
    a, b = np.polyfit(decoded, ref, 1)
    resid = np.abs(ref - (a * decoded + b))
    rng = float(np.ptp(ref)) or 1.0
    return {"n": int(len(ref)), "median": float(np.median(resid)),
            "p90": float(np.percentile(resid, 90)), "max": float(resid.max()),
            "nrmse": float(np.sqrt(np.mean(resid ** 2)) / rng),
            "p90_frac": float(np.percentile(resid, 90) / rng), "range": rng}


def linear_fit_r2(raw: np.ndarray, ref: np.ndarray) -> tuple[float, float, float]:
    """Lag-aligned linear fit  ref ~ offset + scale*raw  -> (scale, offset, r2).

    The shared linear-reconstruction metric. Unlike a Spearman rank correlation
    (monotonic / scale-free), R^2 measures how well the field LINEARLY reconstructs
    the reference, so a coarse or wrong-scale slice that merely moves WITH the signal
    scores measurably lower. Used by bitsearch (the field of record) and surfaced by
    correlate to order the Spearman-tied shortlist. Both inputs isfinite-filtered.
    """
    raw = np.asarray(raw, dtype=np.float64)
    ref = np.asarray(ref, dtype=np.float64)
    valid = np.isfinite(raw) & np.isfinite(ref)
    if valid.sum() < 5 or np.ptp(raw[valid]) == 0:
        return 0.0, 0.0, 0.0
    scale, offset = np.polyfit(raw[valid], ref[valid], 1)
    fitted = offset + scale * raw[valid]
    ss_res = float(np.sum((ref[valid] - fitted) ** 2))
    ss_tot = float(np.sum((ref[valid] - ref[valid].mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return float(scale), float(offset), float(r2)


# ---------------------------------------------------------------------------
# Auto-round a fitted scale/offset to neat OEM values
# ---------------------------------------------------------------------------

# Auto-rounding snaps a fitted scale/offset to neat OEM values - but ONLY when
# doing so barely moves the decode. The gate is a SYSTEMATIC-BIAS BUDGET, not R^2:
# R^2 (and rank correlation) are nearly blind to a small multiplicative scale
# error - a 2% slope change keeps R^2 ~ 0.99 - so they cannot tell a noise-level
# cleanup (true 0.0999 -> 0.1) from a rounding that injects a real, value-
# proportional bias (true ~0.098 -> 0.1, reading ~2% high: the dashboard-vs-OBD
# trap). The bias a rounding introduces over the observed data, as a fraction of
# the signal's range, measures that harm directly.
ROUND_SCALE_TOL = 0.03       # only consider snapping a scale within 3% of a nice value
ROUND_BIAS_BUDGET = 0.01     # auto-apply only if the rounding shifts the decode <= 1% of range


def _snap_offset(off: float, rng: float) -> float:
    """Snap an offset to 0, else the nearest integer, when within ~2% of the
    signal's range; otherwise keep it unchanged."""
    if abs(off) <= 0.02 * rng:
        return 0.0
    if abs(off - round(off)) <= 0.02 * rng:
        return float(round(off))
    return float(off)


def propose_round_calibration(scale: float, offset: float, raw: np.ndarray,
                              ref: np.ndarray, scale_tol: float = ROUND_SCALE_TOL,
                              bias_budget: float = ROUND_BIAS_BUDGET) -> dict | None:
    """Propose neat OEM scale/offset for a fitted line, gated on systematic bias.

    OEM signals almost always use a round scale (m*10^k or 2^-j) and a round
    offset (very often 0). A robust fit against a quantized reference lands NEAR
    those values; this snaps the scale to its nearest nice value (within
    `scale_tol`) and the offset to 0 / nearest integer, then measures the
    worst-case systematic bias the rounding introduces vs the precise fit, across
    the observed data, as a fraction of the signal's range.

    The `auto` flag says whether it is SAFE to apply silently: True when that bias
    is within `bias_budget` (a genuine noise-level cleanup, e.g. 0.0999 -> 0.1),
    False when the rounding would inject a visible systematic bias (a non-round OEM
    scale, or a biased reference such as indicated-vs-true speed) - surface it and
    let the user decide; do NOT apply silently.

    Returns {scale, offset, scale_changed, offset_changed, max_bias, bias_frac,
    auto} or None when there is nothing to round.
    """
    raw = np.asarray(raw, dtype=np.float64)
    ref = np.asarray(ref, dtype=np.float64)
    v = np.isfinite(raw) & np.isfinite(ref)
    raw, ref = raw[v], ref[v]
    if len(raw) < 5 or np.ptp(raw) == 0:
        return None
    rng = abs(scale) * float(np.ptp(raw))            # observed physical span
    if rng == 0:
        return None
    sp = scale_plausibility(scale, tol=scale_tol)
    nice_scale = (-1.0 if scale < 0 else 1.0) * sp["nearest"] if sp["nice"] else float(scale)
    scale_changed = nice_scale != scale
    if scale_changed:
        # scale moved -> the offset MUST be re-derived for the new scale, then rounded
        nice_offset = _snap_offset(float(np.median(ref - nice_scale * raw)), rng)
    else:
        nice_offset = _snap_offset(float(offset), rng)   # offset-only rounding toward a round value
    offset_changed = nice_offset != offset
    if not (scale_changed or offset_changed):
        return None
    diff = (nice_scale - scale) * raw + (nice_offset - offset)
    max_bias = float(np.max(np.abs(diff)))
    bias_frac = max_bias / rng
    return {"scale": nice_scale, "offset": nice_offset,
            "scale_changed": scale_changed, "offset_changed": offset_changed,
            "max_bias": max_bias, "bias_frac": bias_frac,
            "auto": bias_frac <= bias_budget}


# ---------------------------------------------------------------------------
# Decimal-place tidy-up - shorten the raw fitted scale/offset
# ---------------------------------------------------------------------------

# propose_round_calibration snaps a scale to a *neat OEM value* (m*10^k / 2^-j)
# and only when that snap is near-free; it deliberately leaves a genuinely
# non-OEM scale (e.g. a proprietary 0.0013822) as the raw fitted value with its
# full float tail. That tail is just fit noise - no encoder emits 18 significant
# digits - so a final, always-on cleanup rounds scale AND offset to the FEWEST
# decimal places that does not move the decode by more than a tight precision
# budget (default 0.1% of range). Adaptive: it tries 3 decimals, then 4, 5, ...
# and keeps the first that fits, so a coarse-resolution signal rounds to 0.001
# while a fine one is allowed the extra digits it actually needs. This is
# orthogonal to the OEM snap (which runs first): the snap handles "0.0999 -> 0.1",
# this handles "0.0013822 -> 0.00138".
ROUND_DECIMALS_TOL = 0.001   # max worst-case decode bias from the tidy-up, frac of range
ROUND_DECIMALS_MAX = 12      # never keep more than this many decimal places


def _fewest_decimals(value: float, abs_budget: float,
                     max_decimals: int = ROUND_DECIMALS_MAX) -> tuple[float, int]:
    """Round `value` to the fewest decimal places whose rounding error is within
    `abs_budget`. Returns (rounded_value, n_decimals)."""
    if value == 0.0:
        return 0.0, 0
    for d in range(0, max_decimals + 1):
        r = round(value, d)
        if abs(r - value) <= abs_budget:
            return r, d
    return round(value, max_decimals), max_decimals


def propose_decimal_round(scale: float, offset: float, raw: np.ndarray,
                          tol: float = ROUND_DECIMALS_TOL,
                          max_decimals: int = ROUND_DECIMALS_MAX) -> dict | None:
    """Tidy the raw fitted scale/offset to the fewest decimal places without
    losing significant precision.

    The worst-case decoded bias from rounding, over the observed raw range, is
    (new_scale-scale)*raw + (new_offset-offset). We split the `tol` budget (as a
    fraction of the physical range) evenly between scale and offset so the
    *combined* worst case stays within `tol`, then pick the fewest decimals for
    each that fits its half. Unlike propose_round_calibration this does NOT need a
    reference and does NOT snap to a 'nice' value - it just drops the meaningless
    float tail (0.0013822499 -> 0.00138). Returns {scale, offset, scale_decimals,
    offset_decimals, scale_changed, offset_changed, max_bias, bias_frac} or None
    when nothing changes / the span is degenerate.
    """
    raw = np.asarray(raw, dtype=np.float64)
    raw = raw[np.isfinite(raw)]
    if len(raw) < 2 or np.ptp(raw) == 0:
        return None
    rng = abs(scale) * float(np.ptp(raw))            # observed physical span
    if rng == 0:
        return None
    rmax = max(float(np.max(np.abs(raw))), 1e-12)    # raw magnitude scale error rides on
    half = 0.5 * tol * rng
    new_scale, sd = _fewest_decimals(scale, half / rmax, max_decimals)
    new_offset, od = _fewest_decimals(offset, half, max_decimals)
    scale_changed = new_scale != scale
    offset_changed = new_offset != offset
    if not (scale_changed or offset_changed):
        return None
    diff = (new_scale - scale) * raw + (new_offset - offset)
    max_bias = float(np.max(np.abs(diff)))
    return {"scale": new_scale, "offset": new_offset,
            "scale_decimals": sd, "offset_decimals": od,
            "scale_changed": scale_changed, "offset_changed": offset_changed,
            "max_bias": max_bias, "bias_frac": max_bias / rng}


# ---------------------------------------------------------------------------
# Physical-anchor offset re-pin - honour a known rest/zero operating point
# ---------------------------------------------------------------------------

# A free two-stage fit minimises overall squared error; it has no notion that a
# signal MUST hit a known physical value at a known operating point. The most
# universal such anchor is the REST/ZERO state: a speed, flow, current, power,
# torque, ... reads exactly 0 when the system is idle. A quantized and/or laggy
# reference (e.g. integer-km/h OBD speed that lags the fast raw frame through
# transients) biases the low end, and the fit absorbs that into a small non-zero
# offset - so the decode reads e.g. -1.6 km/h while parked. R^2 (and rank
# correlation) are nearly blind to this constant offset, so they cannot gate it -
# but a known anchor can: pin the OFFSET so the rest state decodes to its true
# value. The SHIFT this needs, as a fraction of range, is the gate (mirrors
# auto-round's bias budget): a SMALL shift is a noise-level offset cleanup that
# barely moves R^2 -> auto-apply; a LARGE shift means the decode genuinely
# disagrees with a physical certainty -> the field geometry is probably wrong, so
# FLAG it rather than papering over it with an offset (the user can still force it).
ANCHOR_BIAS_BUDGET = 0.06     # auto-pin only if the needed offset shift <= 6% of range
ANCHOR_ZERO_TOL = 0.05        # auto "rest == 0" only if the rest level sits <= 5% of range from 0
ANCHOR_NEGLIGIBLE = 0.005     # below this (frac of range) the anchor is already honoured


def detect_rest_cluster(ref: np.ndarray, frac_band: float = 0.03,
                        min_n: int = 10, min_frac: float = 0.02) -> tuple | None:
    """Find a dense steady cluster at the LOW (rest) end of a reference series.

    The rest cluster = samples within `frac_band` of the reference range above its
    minimum (a parked/idle/at-rest plateau). It must hold at least
    max(min_n, min_frac*N) samples to count as a genuine operating point rather
    than a transient dip. Returns (mask, ref_level, n) aligned to `ref`, or None.
    """
    ref = np.asarray(ref, dtype=np.float64)
    v = np.isfinite(ref)
    if int(v.sum()) < min_n:
        return None
    r = ref[v]
    rng = float(np.ptp(r))
    if rng == 0:
        return None
    rmin = float(np.min(r))
    in_band = r <= rmin + frac_band * rng
    n = int(in_band.sum())
    if n < max(min_n, int(min_frac * len(r))):
        return None
    ref_level = float(np.median(r[in_band]))
    full = np.zeros(len(ref), dtype=bool)
    full[np.flatnonzero(v)[in_band]] = True
    return full, ref_level, n


def propose_anchor_calibration(scale: float, offset: float, raw: np.ndarray,
                               ref: np.ndarray, anchor_value: float | None = None,
                               bias_budget: float = ANCHOR_BIAS_BUDGET,
                               zero_tol: float = ANCHOR_ZERO_TOL) -> dict | None:
    """Re-pin OFFSET so a known rest/zero physical anchor decodes exactly.

    Finds the reference's dense rest cluster (see `detect_rest_cluster`) and the
    modal raw field value there, then re-fits the line constrained to pass through
    that anchor (re-deriving the slope from the data), so the decode at that state
    equals the anchor's true physical value without injecting a motion bias.

    `anchor_value`: physical value of the rest cluster. If None it is auto-detected
    ONLY for a true-zero rest - the cluster must sit within `zero_tol` of range from
    0 (the near-universal "idle => 0" case: speed, flow, current, power). A non-zero
    rest (e.g. idle RPM ~800) is signal-specific and ambiguous, so it is NOT
    auto-anchored - the caller must pass an explicit `anchor_value`.

    `auto` is True when the required shift is within `bias_budget` of range (a
    noise-level offset cleanup); False when it is large (the decode disagrees with a
    physical certainty => the field is probably wrong - surface it, don't apply
    silently). When the caller passes an explicit anchor it may apply regardless of
    `auto`, but should still report a large shift.

    Returns {scale, offset, old_scale, delta, anchor_value, ref_level, raw_rest,
    current, n_rest, bias_frac, auto} or None when there is no rest cluster /
    nothing to correct.
    """
    raw = np.asarray(raw, dtype=np.float64)
    ref = np.asarray(ref, dtype=np.float64)
    v = np.isfinite(raw) & np.isfinite(ref)
    if int(v.sum()) < 5:
        return None
    rng = abs(float(scale)) * float(np.ptp(raw[v]))
    if rng == 0:
        return None
    rc = detect_rest_cluster(ref)
    if rc is None:
        return None
    rest_mask, ref_level, n_rest = rc
    m = rest_mask & v
    if int(m.sum()) < 5:
        return None
    # Representative raw AT rest = the MODE of the (integer) raw field over the rest
    # cluster, not its median. A loose rest band catches some low-end creep frames
    # whose raw is non-zero and spread thin; at a true steady state the field sits
    # at ONE constant value, so it dominates as a single mode while creep values
    # scatter. The median can be dragged off the true rest reading by that creep
    # (e.g. a parked speed whose rest band medians to raw 13 though it sits at 0);
    # the mode recovers the constant the signal actually rests at.
    vals, counts = np.unique(np.rint(raw[m]).astype(np.int64), return_counts=True)
    raw_rest = float(vals[int(np.argmax(counts))])
    if anchor_value is None:
        if abs(ref_level) > zero_tol * rng:
            return None              # non-zero rest: needs an explicit anchor_value
        target = 0.0
    else:
        target = float(anchor_value)
    current = float(scale) * raw_rest + float(offset)
    delta = target - current
    if abs(delta) <= ANCHOR_NEGLIGIBLE * rng:
        return None                  # already honoured - nothing to do
    bias_frac = abs(delta) / rng
    # Re-fit the line CONSTRAINED to pass through the anchor (x0=raw_rest,
    # y0=target) rather than merely shifting the offset. An offset-only shift keeps
    # the SLOPE that was fitted for the old offset, so the decode then reads
    # systematically biased away from the anchor across the moving range (here a
    # parked-correct line that reads ~2% high in motion). Pinning the slope through
    # the anchor - least-squares of (ref-y0) on (raw-x0) - honours the anchor
    # exactly AND keeps the decode centred on the reference. Sentinels are already
    # masked (NaN) before this, so plain LS is well-posed.
    x, y = raw[v], ref[v]
    dx = x - raw_rest
    denom = float(np.sum(dx * dx))
    if denom <= 0:
        return None
    new_scale = float(np.sum(dx * (y - target)) / denom)
    new_offset = target - new_scale * raw_rest
    return {"scale": new_scale, "offset": new_offset, "old_scale": float(scale),
            "delta": delta, "anchor_value": target, "ref_level": ref_level,
            "raw_rest": raw_rest, "current": current, "n_rest": n_rest,
            "bias_frac": bias_frac, "auto": bias_frac <= bias_budget}


# ---------------------------------------------------------------------------
# Plotting - CSS Electronics brand colors (visual identity palette)
# ---------------------------------------------------------------------------
# These four are the canonical brand hexes (blue/orange/green/red) and are the
# SINGLE SOURCE OF TRUTH for every plot in this skill - all scripts import these
# rather than hardcoding hex, so the whole suite restyles from one place. Light
# tints derived below (moving-shade, heatmap gradient mid-stops) harmonise with
# them; greys (#444/#9aa0a6/#eee) and near-black label text are neutral, not brand.

COLOR_REFERENCE = "#3d85c6"   # blue   - the trusted reference series
COLOR_DECODED = "#ff9900"     # orange - the candidate decoded field
COLOR_MOVING_SHADE = "#ffe0b3"  # pale orange tint of COLOR_DECODED (moving-segment wash)


def plot_reference_overlay(out_path, title: str, t_rel: np.ndarray,
                           decoded: np.ndarray, unit: str = "",
                           ref_t_rel: np.ndarray | None = None,
                           ref_v: np.ndarray | None = None,
                           decoded_label: str = "decoded",
                           reference_label: str = "reference",
                           moving_mask: np.ndarray | None = None,
                           extreme_mask: np.ndarray | None = None,
                           window_spans: list | None = None) -> None:
    """Overlay a decoded signal and its reference on ONE shared Y axis.

    Both series carry the SAME physical unit once the field is calibrated, so a
    single axis is the whole point - any vertical gap is a real disagreement the
    user can see at a glance (a twin axis would hide a wrong scale). Reference is
    drawn in brand blue, decoded in brand orange, with a legend. The Y limits are
    set robustly from the bulk of the data so a stray sentinel/outlier (drawn but
    annotated) can never squash the curve.

    `window_spans` (holds windows-only): list of (t0_rel, t1_rel, value, steady,
    ptp). Each is shaded as a SAMPLED window - green if the decoded signal held
    still inside it, red if a transition leaked in - with a reference tick at its
    known value, so the user sees exactly what was sampled and whether it was
    steady. Unshaded time is recorded-but-unlabelled motion.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_path = str(out_path)
    fig, ax = plt.subplots(figsize=(11, 4.5))
    if window_spans:
        labelled_g = labelled_r = False
        for t0, t1, v, steady, _ptp in window_spans:
            color = COLOR_GOOD if steady else COLOR_BAD
            lbl = None
            if steady and not labelled_g:
                lbl, labelled_g = "sampled window (steady)", True
            elif not steady and not labelled_r:
                lbl, labelled_r = "sampled window (transition leaked in)", True
            ax.axvspan(t0, t1, color=color, alpha=0.18, lw=0, label=lbl)
            ax.hlines(v, t0, t1, color=COLOR_REFERENCE, lw=2.2, zorder=5)
    if moving_mask is not None and np.any(moving_mask):
        in_seg, seg_start = False, 0.0
        for i in range(len(t_rel)):
            if moving_mask[i] and not in_seg:
                in_seg, seg_start = True, float(t_rel[i])
            elif not moving_mask[i] and in_seg:
                ax.axvspan(seg_start, float(t_rel[i]), color=COLOR_MOVING_SHADE,
                           alpha=0.5, lw=0)
                in_seg = False
        if in_seg:
            ax.axvspan(seg_start, float(t_rel[-1]), color=COLOR_MOVING_SHADE,
                       alpha=0.5, lw=0)
    if ref_t_rel is not None and ref_v is not None and len(ref_t_rel) >= 2:
        ax.step(ref_t_rel, ref_v, where="post", color=COLOR_REFERENCE,
                lw=1.8, label=reference_label)
    ax.step(t_rel, decoded, where="post", color=COLOR_DECODED, lw=1.4,
            alpha=0.9, label=decoded_label)
    # robust Y limits from the bulk of BOTH series (ignore sentinels/outliers)
    pool = [decoded]
    if ref_v is not None and len(ref_v):
        pool.append(np.asarray(ref_v, dtype=np.float64))
    allv = np.concatenate([p[np.isfinite(p)] for p in pool if np.any(np.isfinite(p))]) \
        if pool else np.array([])
    if allv.size:
        lo, hi = (float(v) for v in np.percentile(allv, [0.5, 99.5]))
        if hi > lo:
            pad = 0.05 * (hi - lo)
            ax.set_ylim(lo - pad, hi + pad)
    suffix = ""
    if extreme_mask is not None and np.any(extreme_mask):
        suffix = f"  ({int(np.sum(extreme_mask))} extreme sample(s) off-scale)"
    ax.set_xlabel("time (s)" + ("  (shaded = moving / unshaded = parked)"
                                if moving_mask is not None else ""))
    ax.set_ylabel(f"[{unit}]" if unit else "value")
    ax.set_title(title + suffix)
    ax.grid(alpha=0.3)
    ax.legend(loc="upper right", framealpha=0.9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Analysis-step visualizations (heatmaps / fit diagnostics) - brand-styled,
# auto-emitted by survey / correlate / bitsearch / build_dbc into a per-signal
# analysis-plots/ folder. They reuse the same Agg setup + brand colors so every
# step's PNG looks consistent (for human candidate-spotting and for blog posts).
# ---------------------------------------------------------------------------

COLOR_GOOD = "#10cba9"     # green  - strong fit / high R^2 / applied rounding
COLOR_BAD = "#ff6666"      # red    - poor / flagged / sentinel / rejected rounding
COLOR_WINNER = COLOR_DECODED   # orange box around the chosen field


def resolve_plots_dir(arg, fallback: str = "temp-output/analysis-plots"):
    """Pick + create the analysis-plots output dir (explicit arg, else fallback)."""
    from pathlib import Path
    d = Path(arg) if arg else Path(fallback)
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_survey_json(trace_path):
    """Default path for survey.py's machine-readable JSON: always under
    temp-output/ (a working file), NEVER next to the trace - the offline trace is
    often the user's read-only input log (e.g. in TEMP/), which must not be
    polluted. Defined here so the writer (survey.py) and the reader
    (correlate.flagged_bytes) derive the SAME path and can't drift.

    label = trace stem with any leading 'trace_' stripped, matching the survey
    heatmap's filename (so survey_<label>.json sits beside 1-survey_..._<label>.png
    conceptually)."""
    from pathlib import Path
    label = Path(trace_path).stem.replace("trace_", "")
    return Path("temp-output") / f"survey_{label}.json"


def _brand_cmap(kind: str = "score"):
    """Sequential brand colormap (white -> blue for score/activity, white -> green
    for R^2). NaN/padded cells render light grey via set_bad, so an absent
    candidate or a shorter payload reads as 'no data', NOT as 'zero'."""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.colors import LinearSegmentedColormap
    stops = (["#ffffff", "#bfeee4", COLOR_GOOD] if kind == "r2"
             else ["#ffffff", "#cfe0f2", COLOR_REFERENCE])
    cmap = LinearSegmentedColormap.from_list(f"brand_{kind}", stops)
    cmap.set_bad("#eeeeee")
    return cmap


def plot_bus_activity(out_path, rows, rates_by_id, *,
                      title="CAN bus bit-activity", max_ids=None) -> None:
    """Heatmap of per-bit flip-rate across CAN IDs (rows) x bit index (cols).

    rows        : survey row dicts (id, id_hex, length, counter_bytes,
                  checksum_bytes, flag_bits, field_map).
    rates_by_id : {can_id:int -> np.ndarray flip-rate per LSB-first bit}.

    Counter bytes (red underline), checksum-like bytes (hatch), constant/flag bits
    (grey dots) and proposed field-map starts (orange ticks) are overlaid so a human
    can see at a glance which IDs/bytes carry varying signals (candidate-rich) and
    spot related signals. Bit index is LSB-first (same as bitsearch/build_dbc)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch, Rectangle
    from matplotlib.lines import Line2D

    rows = [r for r in rows if r.get("length", 0) > 0]
    if not rows:
        return
    if max_ids and len(rows) > max_ids:
        rows = sorted(rows, key=lambda r: -float(np.nansum(
            np.asarray(rates_by_id.get(r["id"], [0.0]), float))))[:max_ids]
    rows = sorted(rows, key=lambda r: r["id"])
    maxlen = max(r["length"] for r in rows)
    nbits, n = maxlen * 8, len(rows)
    grid = np.full((n, nbits), np.nan)
    for i, r in enumerate(rows):
        rate = np.asarray(rates_by_id.get(r["id"], []), dtype=float)
        grid[i, :min(nbits, rate.size)] = rate[:nbits]

    fig, ax = plt.subplots(figsize=(max(10, nbits * 0.16), max(3.2, n * 0.30)))
    im = ax.imshow(grid, aspect="auto", cmap=_brand_cmap("score"),
                   vmin=0.0, vmax=1.0, interpolation="nearest")
    for b in range(maxlen + 1):
        ax.axvline(b * 8 - 0.5, color="white", lw=1.0)
    for i, r in enumerate(rows):
        for bb in r.get("counter_bytes", []):
            ax.plot([bb * 8 - 0.5, bb * 8 + 7.5], [i + 0.42, i + 0.42],
                    color=COLOR_BAD, lw=2.5, solid_capstyle="butt")
        for bb in r.get("checksum_bytes", []):
            ax.add_patch(Rectangle((bb * 8 - 0.5, i - 0.5), 8, 1, fill=False,
                                   hatch="///", edgecolor="#9aa0a6", lw=0.0))
        for k in r.get("flag_bits", []):
            if k < nbits:
                ax.plot(k, i, marker=".", color="#444444", ms=3)
        for f in r.get("field_map", []):
            s = f["start_bit"]
            if s < nbits:
                ax.plot([s - 0.5, s - 0.5], [i - 0.5, i + 0.5],
                        color=COLOR_WINNER, lw=1.6)
    ax.set_yticks(range(n))
    ax.set_yticklabels([f"0x{r['id_hex']}" for r in rows], fontsize=7)
    ax.set_xticks(range(0, nbits, 8))
    ax.set_xticklabels([f"B{b}" for b in range(maxlen)], fontsize=7)
    ax.set_xlabel("bit index (LSB-first; B = byte boundary)")
    ax.set_ylabel("CAN ID")
    ax.set_title(title)
    fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01).set_label("bit flip-rate")
    legend = [Line2D([0], [0], color=COLOR_BAD, lw=2.5, label="counter byte"),
              Patch(facecolor="white", edgecolor="#9aa0a6", hatch="///",
                    label="checksum-like"),
              Line2D([0], [0], color=COLOR_WINNER, lw=1.6, label="field-map start"),
              Line2D([0], [0], marker=".", color="#444444", lw=0,
                     label="constant/flag bit")]
    ax.legend(handles=legend, loc="upper center", bbox_to_anchor=(0.5, -0.075),
              ncol=4, fontsize=7, framealpha=0.9)

    # Footnote spelling out what the plot shows, so its role (a static, whole-capture
    # version of a SavvyCAN-style "fade/stale byte" view) is clear at a glance.
    note = ("Per-bit flip-rate over the whole capture: dark = bit toggles often, "
            "light = rarely, blank = constant, grey = bit absent in a shorter frame.\n"
            "Shows which IDs/bytes carry varying signals - counter bytes are excluded "
            "from correlation, checksums are a hint, field-map starts seed bitsearch.")
    fig.text(0.5, 0.005, note, ha="center", va="top", fontsize=7, color="0.35")

    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def plot_correlation_heatmap(out_path, results, kind, *, winner=None,
                             title=None) -> None:
    """Best correlation score per (ID x byte) [continuous] or (ID x bit)
    [discrete]; the winning cell is boxed. Surfaces other plausible candidate
    IDs/bytes alongside the winner."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    results = [r for r in results if r]
    if not results:
        return
    ids = sorted({r["id"] for r in results})
    id_row = {cid: i for i, cid in enumerate(ids)}
    if kind == "continuous":
        colkey, xlabel = "byte", "byte offset"
        clabel = "windowed Spearman |rho| (best over width/order/sign)"
    else:
        colkey, xlabel = "bit", "bit index (LSB-first)"
        clabel = "change-near-event lift"
    ncol = max(r[colkey] for r in results) + 1
    grid = np.full((len(ids), ncol), np.nan)
    for r in results:
        i, j, s = id_row[r["id"]], r[colkey], float(r["score"])
        if np.isnan(grid[i, j]) or s > grid[i, j]:
            grid[i, j] = s

    w = min(22, max(8, ncol * 0.5))
    fig, ax = plt.subplots(figsize=(w, max(3.2, len(ids) * 0.30)))
    vmax = max(1.0, float(np.nanmax(grid)))
    im = ax.imshow(grid, aspect="auto", cmap=_brand_cmap("score"), vmin=0.0,
                   vmax=vmax, interpolation="nearest")

    # Print the score inside every scored cell so the winner is legible from the
    # numbers, not just the shade. Skip on very wide frames (e.g. a CAN FD byte
    # grid, or a bit-level discrete grid) where per-cell text would collide.
    if ncol <= 24:
        for i in range(len(ids)):
            for j in range(ncol):
                v = grid[i, j]
                if not np.isfinite(v):
                    continue
                # 3 decimals: near-tied candidates across IDs (e.g. a CAN FD frame
                # mirroring a classical frame) differ in the 3rd digit, so 2 dp would
                # make them read identically (0.95 vs 0.95 instead of 0.946 vs 0.953).
                ax.text(j, i, f"{v:.3f}", ha="center", va="center", fontsize=5.5,
                        color=("white" if v >= 0.6 * vmax else "#1a1a1a"))

    cap = ""
    if winner is not None and winner.get("id") in id_row:
        i, j = id_row[winner["id"]], winner[colkey]
        # Two nested borders distinguish START byte from field LENGTH: a solid
        # box on the start byte, and (for a multi-byte field) a thinner dotted
        # box laid on top spanning the full width (a 2-byte field covers bytes
        # b..b+1). So the highlight shows both where the field begins and how
        # wide the field we decode actually is.
        span = winner.get("width", 1) if kind == "continuous" else 1
        ax.add_patch(Rectangle((j - 0.5, i - 0.5), 1, 1, fill=False,
                               edgecolor=COLOR_WINNER, lw=2.5))
        if span > 1:
            ax.add_patch(Rectangle((j - 0.5, i - 0.5), span, 1, fill=False,
                                   edgecolor=COLOR_WINNER, lw=1.4, linestyle=":"))
        if kind == "continuous":
            b, wid = winner["byte"], winner["width"]
            where = f"byte {b}" if wid == 1 else f"bytes {b}-{b + wid - 1}"
            cap = (f"winner 0x{winner['id_hex']}  {where} "
                   f"({wid * 8}-bit {winner['order']}-endian)  "
                   f"lag={winner.get('lag_s', '?')}s  |rho|={winner['score']}")
        else:
            cap = f"winner 0x{winner['id_hex']}  bit {winner['bit']}  lift={winner['score']}"
    step = max(1, ncol // 32)
    ax.set_yticks(range(len(ids)))
    ax.set_yticklabels([f"0x{cid:X}" for cid in ids], fontsize=7)
    ax.set_xticks(range(0, ncol, step))
    ax.set_xticklabels(range(0, ncol, step), fontsize=7)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("CAN ID")
    ax.set_title((title or f"Correlation vs reference ({kind})")
                 + (("\n" + cap) if cap else ""))
    fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02).set_label(clabel)

    # Footnote spelling out what this plot is for, so its coarse role (locate the
    # ID/region) vs bitsearch's job (pin the exact field) is unambiguous.
    if kind == "continuous":
        note = ("Coarse byte-aligned triage: each cell = best |rho| for a 1-2 byte "
                "field STARTING at that byte (best over width / endianness / sign). "
                "Locate the ID & byte region here;\nbitsearch then resolves the exact "
                "start-bit, length and endianness. Winner: solid box = start byte, "
                "dotted box = full field span.\nColumns = scored byte offsets only "
                "(varying, non-counter bytes up to --max-width), so a wide CAN FD frame "
                "may not show every byte of its DLC.")
    else:
        note = ("Each cell = change-near-event lift for that single bit.\n"
                "Use it to locate the ID & bit that toggles with the event.")
    fig.text(0.5, 0.005, note, ha="center", va="top", fontsize=7, color="0.35")

    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def plot_bitsearch_grid(out_path, results, can_id, *, winner=None,
                        title=None) -> None:
    """Two-panel R^2 heatmap of the bitsearch space for one ID (little-endian
    start_bit x length, big-endian byte x width) plus a max-R^2-vs-length 'knee'
    curve. Visualizes WHY a field won and the parsimony jump-then-plateau."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.gridspec import GridSpec
    from matplotlib.patches import Rectangle

    results = [r for r in results if r]
    cmap = _brand_cmap("r2")
    fig = plt.figure(figsize=(13, 7))
    gs = GridSpec(2, 2, height_ratios=[3, 1], hspace=0.45, wspace=0.25,
                  figure=fig)

    def _panel(ax, subset, rowkey, colkey, ylabel, xlabel, ptitle):
        if not subset:
            ax.text(0.5, 0.5, f"no {ptitle} candidates", ha="center", va="center")
            ax.axis("off")
            return
        rvals = sorted({r[rowkey] for r in subset})
        cvals = sorted({r[colkey] for r in subset})
        ri = {v: i for i, v in enumerate(rvals)}
        ci = {v: j for j, v in enumerate(cvals)}
        g = np.full((len(rvals), len(cvals)), np.nan)
        for r in subset:
            i, j, v = ri[r[rowkey]], ci[r[colkey]], max(0.0, float(r["r2"]))
            if np.isnan(g[i, j]) or v > g[i, j]:
                g[i, j] = v
        im = ax.imshow(g, aspect="auto", cmap=cmap, vmin=0, vmax=1,
                       interpolation="nearest")
        # Print the R^2 inside each cell when the panel is small enough to stay
        # legible (the big-endian byte x width panel; the little-endian start-bit x
        # length panel has far too many cells, so it keeps colour only).
        if len(rvals) * len(cvals) <= 48:
            for i in range(len(rvals)):
                for j in range(len(cvals)):
                    v = g[i, j]
                    if not np.isfinite(v):
                        continue
                    ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=6,
                            color=("white" if v >= 0.6 else "#1a1a1a"))
        ax.set_yticks(range(len(rvals)))
        ax.set_yticklabels(rvals, fontsize=6)
        ax.set_xticks(range(len(cvals)))
        ax.set_xticklabels(cvals, fontsize=7)
        ax.set_ylabel(ylabel)
        ax.set_xlabel(xlabel)
        ax.set_title(ptitle)
        fig.colorbar(im, ax=ax, fraction=0.035, pad=0.02).set_label("linear-fit R^2")
        if winner and winner.get(rowkey) in ri and winner.get(colkey) in ci \
                and ((winner["order"] == "little") == (rowkey == "start_bit")):
            ax.add_patch(Rectangle((ci[winner[colkey]] - 0.5,
                                    ri[winner[rowkey]] - 0.5), 1, 1, fill=False,
                                   edgecolor=COLOR_WINNER, lw=2.5))

    _panel(fig.add_subplot(gs[0, 0]),
           [r for r in results if r["order"] == "little"],
           "start_bit", "length", "start bit (LSB-first)", "length (bits)",
           "little-endian (Intel)")
    _panel(fig.add_subplot(gs[0, 1]),
           [r for r in results if r["order"] == "big"],
           "byte", "width", "start byte", "width (bytes)", "big-endian (Motorola)")

    # parsimony knee: R^2 vs field length within the WINNER's nested family (same
    # start, growing width). The true width is the knee - R^2 jumps when the
    # field's own bytes are included, then plateaus as the read spills into the
    # neighbour. (Global max-per-length would be noisy with unrelated fields.)
    axK = fig.add_subplot(gs[1, :])
    src, xlab = [], "field length (bits)"
    if winner and winner["order"] == "big":
        src = [(r["width"] * 8, r["r2"]) for r in results
               if r["order"] == "big" and r.get("byte") == winner.get("byte")]
        xlab = f"field length (bits), big-endian at start byte {winner.get('byte')}"
    elif winner and winner["order"] == "little":
        src = [(r["length"], r["r2"]) for r in results
               if r["order"] == "little" and r.get("start_bit") == winner.get("start_bit")]
        xlab = f"field length (bits), little-endian at start bit {winner.get('start_bit')}"
    if not src:
        src = [(r["length"], r["r2"]) for r in results]
    fam = {}
    for L, v in src:
        fam[L] = max(fam.get(L, 0.0), max(0.0, float(v)))
    Ls = sorted(fam)
    if Ls:
        vals = [fam[L] for L in Ls]
        axK.plot(Ls, vals, "-o", color=COLOR_REFERENCE, ms=5)
        if len(Ls) <= 12:
            for L, v in zip(Ls, vals):
                axK.annotate(f"{v:.3f}", (L, v), textcoords="offset points",
                             xytext=(0, 6), ha="center", fontsize=7, color="0.25")
        if winner:
            axK.axvline(winner["length"], color=COLOR_WINNER, lw=1.8,
                        label=f"winner len {winner['length']}")
            axK.legend(fontsize=7, loc="lower right")
        # Autoscale Y to the data band (NOT forced to 0): these R^2 values cluster
        # near 1, so a 0-based axis flattens them into an unreadable line at the top.
        lo, hi = min(vals), max(vals)
        pad = max(0.01, (hi - lo) * 0.25)
        axK.set_ylim(max(0.0, lo - pad), min(1.05, hi + pad + 0.03))
    else:
        axK.set_ylim(0, 1.02)
    axK.grid(alpha=0.3)
    axK.set_xlabel(xlab)
    axK.set_ylabel("R^2")
    axK.set_title("R^2 vs field length at the winning start byte "
                  "(Y zoomed to the data; jumps at the true width, then plateaus)",
                  fontsize=9)

    sup = title or (f"bitsearch: exhaustive field scan - ID 0x{can_id:X}  "
                    "(every start-bit x length x endianness fitted to the reference)")
    if winner:
        if winner["order"] == "big":
            geom = (f"big-endian byte {winner['byte']}, width {winner['width']} "
                    f"({winner['length']}-bit)")
        else:
            geom = f"little-endian start-bit {winner['start_bit']}, {winner['length']}-bit"
        sup += f"\nwinner: {geom}   R^2 {winner['r2']}"
        if winner.get("refined_from"):
            sup += (f"   [resolution-refined from {winner['refined_from']} via "
                    "transition data - see 3b-resolution-refine.png]")
    fig.suptitle(sup)

    note = ("Each cell = linear-fit R^2 of that candidate field vs the reference "
            "(teal = higher); winner boxed. Left: little-endian (start-bit x length); "
            "right: big-endian (start-byte x width).\n"
            "Bottom: R^2 vs field length at the winner's start byte - it jumps at the "
            "true width, then plateaus as the read spills into the next byte (parsimony "
            "keeps the shortest field that fits).")
    fig.text(0.5, 0.005, note, ha="center", va="top", fontsize=7, color="0.35")

    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def _longest_run(mask: np.ndarray) -> tuple[int, int]:
    """(start, end-exclusive) of the longest contiguous True run; (0, len) if none."""
    best_lo = best_hi = best = 0
    i, n = 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            if j - i > best:
                best, best_lo, best_hi = j - i, i, j
            i = j
        else:
            i += 1
    return (best_lo, best_hi) if best else (0, n)


def plot_resolution_refine(out_path, t, narrow_raw, refined_raw, *,
                           narrow_label: str, refined_label: str,
                           title=None) -> None:
    """Show WHY the resolution refinement grew a field: the narrow (parsimony) slice
    STAIRCASES through a between-hold transition while the refined wider field RAMPS
    smoothly. Each series is min-max normalized to [0,1] (they have different bit
    widths/scales) and drawn over the longest transition window; the legend reports
    each field's distinct-value count during motion (the discriminator). This is the
    reference-free evidence the R^2 grid cannot show, since both fields fit the steady
    holds ~equally - the difference only appears while the signal is moving.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = np.asarray(t, np.float64)
    narrow = np.asarray(narrow_raw, np.float64)
    refined = np.asarray(refined_raw, np.float64)
    motion = _motion_mask(narrow, t, 0.5)
    n_res, r_res = _field_resolution(narrow, motion), _field_resolution(refined, motion)

    lo, hi = _longest_run(motion)                 # zoom to one clean transition
    pad = max(1, (hi - lo) // 4)
    lo, hi = max(0, lo - pad), min(len(t), hi + pad)
    sl = slice(lo, hi)
    trel = t[sl] - t[lo]

    def norm(v):
        v = v[sl]
        rng = float(np.ptp(v))
        return (v - v.min()) / rng if rng else np.zeros_like(v)

    fig, ax = plt.subplots(figsize=(11, 4.2))
    ax.step(trel, norm(narrow), where="post", color=COLOR_BAD, lw=1.7,
            label=f"{narrow_label}  (staircase: {n_res} distinct values in motion)")
    ax.plot(trel, norm(refined), color=COLOR_GOOD, lw=1.7,
            label=f"{refined_label}  (smooth ramp: {r_res} distinct values in motion)")
    ax.set_xlabel("time (s)  -  one transition between holds")
    ax.set_ylabel("field value (normalized 0-1)")
    ax.set_title(title or "resolution refinement: narrow slice staircases vs refined "
                 "field ramps through the transition")
    ax.grid(alpha=0.3)
    ax.legend(loc="best", framealpha=0.9, fontsize=9)
    fig.text(0.5, 0.005, "The steady holds alone cannot tell these apart (both fit the "
             "held levels). The transition does: the refined field steps finely (many "
             "distinct values), the narrow slice jumps coarsely. Reference-free.",
             ha="center", va="top", fontsize=7, color="0.35")
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def plot_bit_cascade(out_path, rate, *, can_id, start_bit, length, aligned=None,
                     title=None) -> None:
    """Per-bit flip-rate bar chart for one ID's winning field - the reference-free
    'proper resolution' evidence.

    Each bar is a bit's flip-rate (fraction of frames where it toggles). The EXERCISED
    field (the bits that actually moved, located by the cascade) is shaded orange;
    constant bits are pale; and - when a byte-aligned snap is proposed - the inferred
    (constant, unexercised) filler bits are hatched. Within one contiguous little-endian
    integer field the rate roughly DOUBLES toward the LSB (the cascade) and collapses to
    0 at the field edges - so the plot shows at a glance why the chosen width is right
    and which flanking bits are merely inferred."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    rate = np.asarray(rate, float)
    nb = len(rate)
    msb = start_bit + length - 1
    ex = set(range(start_bit, msb + 1))
    inferred = set()
    if aligned:
        a, al = aligned
        inferred = set(range(a, a + al)) - ex
    colors = [COLOR_DECODED if k in ex else "#cfcfcf" if k in inferred else "#ececec"
              for k in range(nb)]
    # focus the x-axis near the field (+ a little context) so the cascade is legible
    far = max([msb] + ([a + al - 1] if aligned else [])
              + [k for k in range(nb) if rate[k] > 1e-6])
    xmax = min(nb, far + 4)
    fig, ax = plt.subplots(figsize=(max(8, xmax * 0.30), 4.0))
    if aligned:                                # shade the proposed byte-aligned extent
        ax.axvspan(a - 0.5, a + al - 0.5, color=COLOR_DECODED, alpha=0.08, zorder=0)
    bars = ax.bar(range(nb), rate, color=colors, edgecolor="#888888", linewidth=0.4)
    for k in inferred:
        bars[k].set_hatch("///")
    for b in range(0, nb + 1, 8):
        ax.axvline(b - 0.5, color="0.7", lw=0.8, ls=":")
    ax.set_xlim(-0.7, xmax - 0.3)
    ax.set_xlabel("bit index (LSB-first; dotted = byte boundary)")
    ax.set_ylabel("flip-rate (fraction of frames)")
    ax.set_xticks(range(0, xmax, 2))
    ax.tick_params(labelsize=7)
    ax.set_title(title or f"bit-activity cascade - ID 0x{can_id:X}: field {start_bit}|{length}")
    ax.grid(axis="y", alpha=0.3)
    legend = [Patch(facecolor=COLOR_DECODED, label=f"exercised field {start_bit}|{length}")]
    if inferred:
        a, al = aligned
        legend.append(Patch(facecolor="#cfcfcf", hatch="///",
                            label=f"inferred filler (byte-align -> {a}|{al})"))
    legend.append(Patch(facecolor="#ececec", label="constant / other"))
    ax.legend(handles=legend, fontsize=8, loc="upper right", framealpha=0.9)
    note = ("Each bar = one bit's flip-rate (fraction of frames it toggled); bits are "
            "LSB-first, dotted lines mark byte boundaries. A single little-endian field "
            "shows a CASCADE: the LSB toggles most, each higher bit about half as often, "
            "collapsing to ~0 at both ends.\n"
            "The orange cascade is the field width found from bus activity alone (no "
            "reference); pale hatched flanks are inferred filler (sub-resolution bits "
            "below, unexercised high bits above); an isolated bar cluster beyond a "
            "constant gap is a neighbouring signal, not part of this field.")
    fig.text(0.5, 0.01, note, ha="center", va="top", fontsize=7, color="0.35")
    fig.tight_layout(rect=(0, 0.09, 1, 1))
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def plot_fit_diagnostic(out_path, raw, ref, scale, offset, *, name="Signal",
                        unit="", extreme_mask=None, round_candidate=None,
                        r2=None) -> None:
    """raw-vs-reference scatter + fitted line (+ rounded-candidate line) and a
    residual-vs-value panel. The rounded line is green if auto-applied, red if it
    would inject bias - so the indicated-vs-true / bias-gate story is visible."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.6))
    if ref is None:
        ax1.text(0.5, 0.5, "no reference supplied\n(scale/offset override)",
                 ha="center", va="center")
        ax1.axis("off")
        ax2.axis("off")
        fig.savefig(out_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return
    raw = np.asarray(raw, float)
    ref = np.asarray(ref, float)
    finite = np.isfinite(raw) & np.isfinite(ref)
    ext = (np.zeros_like(finite) if extreme_mask is None
           else np.asarray(extreme_mask, bool))
    keep = finite & ~ext
    ax1.scatter(raw[keep], ref[keep], s=6, alpha=0.35, color=COLOR_REFERENCE,
                edgecolors="none", label="frames")
    if np.any(finite & ext):
        ax1.scatter(raw[finite & ext], ref[finite & ext], s=22, color=COLOR_BAD,
                    marker="x", label="extreme / sentinel")
    if np.any(keep):
        xs = np.array([np.nanmin(raw[keep]), np.nanmax(raw[keep])])
        ax1.plot(xs, offset + scale * xs, color=COLOR_DECODED, lw=2,
                 label=f"fit {scale:.5g}*raw{offset:+.3g}"
                       + (f"  R^2={r2:.4f}" if r2 is not None else ""))
        if round_candidate:
            rs, ro = round_candidate["scale"], round_candidate["offset"]
            good = round_candidate.get("auto", False)
            tag = ("applied" if good
                   else f"NOT applied, +{100 * round_candidate['bias_frac']:.1f}% bias")
            ax1.plot(xs, ro + rs * xs, "--", lw=1.8,
                     color=(COLOR_GOOD if good else COLOR_BAD),
                     label=f"round {rs:g}*raw{ro:+g} ({tag})")
    ax1.set_xlabel("raw field value")
    ax1.set_ylabel(f"reference [{unit}]")
    ax1.set_title("reference vs raw field value (+ fitted line)")
    ax1.grid(alpha=0.3)
    ax1.legend(fontsize=7, loc="best")

    decoded = offset + scale * raw
    resid = ref - decoded
    ax2.scatter(decoded[keep], resid[keep], s=6, alpha=0.35,
                color=COLOR_REFERENCE, edgecolors="none")
    ax2.axhline(0, color="#444444", lw=1)
    ax2.set_xlabel(f"decoded [{unit}]")
    ax2.set_ylabel(f"reference - decoded [{unit}]")
    ax2.set_title("residuals: reference - decoded (flat band = unbiased)")
    ax2.grid(alpha=0.3)

    sup = f"fit diagnostic - {name}: deriving the DBC scale & offset"
    sub = (f"model: reference = scale*raw + offset    |    "
           f"fitted scale={scale:g}, offset={offset:g}"
           + (f", R^2={r2:.4f}" if r2 is not None else ""))
    fig.suptitle(sup + "\n" + sub, fontsize=11)
    note = ("Left: each frame's raw field value (x) vs the reference (y), with the fitted "
            "line; a green/red dashed line, if shown, is a round-number scale/offset "
            "candidate (green = applied, red = rejected for injecting bias).\n"
            "Right: residuals (reference - decoded) vs decoded - a flat band at 0 means "
            "the scale & offset are right; a slope or wedge reveals a systematic bias.")
    fig.text(0.5, 0.01, note, ha="center", va="top", fontsize=7, color="0.35")
    fig.tight_layout(rect=(0, 0.08, 1, 0.86))
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Selftest: extraction <-> cantools agreement (no hardware)
# ---------------------------------------------------------------------------

def _selftest() -> int:
    rng = np.random.default_rng(0)
    # (payload_len, [(order, byte_off/bit_start, length, signed), ...])
    suites = [
        (8, [("little", 0, 8, False), ("little", 8, 16, False),
             ("little", 8, 16, True), ("little", 11, 5, False),
             ("big", 0, 1, False), ("big", 2, 2, False), ("big", 2, 2, True)]),
        # CAN FD: 16-byte payload, fields beyond bit 63
        (16, [("little", 80, 16, False),   # byte 10, 16-bit Intel
              ("little", 96, 12, False),   # bit 96 (byte 12), 12-bit Intel
              ("little", 100, 16, True),   # straddles bytes, signed
              ("big", 10, 2, False),       # byte 10, 2-byte Motorola
              ("big", 10, 2, True)]),
    ]
    ok = True
    for payload_len, cases in suites:
        payloads = [bytes(rng.integers(0, 256, payload_len, dtype=np.uint8))
                    for _ in range(200)]
        le = np.array([int.from_bytes(p, "little") for p in payloads], dtype=object)
        be = np.array([int.from_bytes(p, "big") for p in payloads], dtype=object)
        for order, off, length, signed in cases:
            if order == "little":
                raw = extract_le(le, off, length)
                ct_start, ct_len = off, length
            else:
                raw = extract_be(be, payload_len, off, length)  # off=byte, length=width
                ct_start, ct_len = off * 8 + 7, length * 8
            raw = apply_sign(raw, ct_len if order == "big" else length, signed)
            db = make_single_signal_db(
                "S", 0x123, False, payload_len, ct_start, ct_len, order, signed)
            msg = db.get_message_by_name("MSG_0x123")
            mismatch = 0
            for i, p in enumerate(payloads):
                dec = msg.decode(p, allow_truncated=True)["S"]
                if abs(float(dec) - float(raw[i])) > 1e-9:
                    mismatch += 1
            status = "OK" if mismatch == 0 else f"FAIL ({mismatch}/200)"
            if mismatch:
                ok = False
            print(f"  len{payload_len:>2} {order:6} off={off:>3} len={length:>2} "
                  f"signed={signed!s:5} -> {status}")

    # --- plausibility: a full 16-bit ramp must out-score its wrapping low byte ---
    ramp16 = np.linspace(1000, 60000, 500)
    low8 = ramp16.astype(np.int64) & 0xFF          # wraps every 256 counts
    p_full = plausibility(ramp16, 16)["score"]
    p_slice = plausibility(low8, 8)["score"]
    p_ok = p_full > p_slice and p_full > 0.5 and p_slice < p_full
    ok = ok and p_ok
    print(f"  plausibility ramp16={p_full:.3f} > lowbyte8={p_slice:.3f} -> "
          f"{'OK' if p_ok else 'FAIL'}")

    # --- scale_plausibility: flag 1/2593, accept genuine OEM scales ---
    nice_cases = [1e-6, 1e-7, 0.001, 0.125, 0.5, 1.0, 0.25, 2.5]
    bad_cases = [-1 / 2593.0, -0.0003855691, 1 / 777.0]
    sp_ok = all(scale_plausibility(s)["nice"] for s in nice_cases) and \
        all(not scale_plausibility(s)["nice"] for s in bad_cases)
    ok = ok and sp_ok
    print(f"  scale_plausibility nice/bad classification -> "
          f"{'OK' if sp_ok else 'FAIL'}")

    # --- signed windowed-Spearman: recovers correlation sign ---
    x = np.linspace(0, 10, 300)
    _, s_pos = _windowed_spearman_signed(x, 2 * x + 1, N_WINDOWS)
    _, s_neg = _windowed_spearman_signed(x, -3 * x + 5, N_WINDOWS)
    sgn_ok = s_pos == 1 and s_neg == -1
    ok = ok and sgn_ok
    print(f"  windowed-Spearman sign (+1/{s_pos}, -1/{s_neg}) -> "
          f"{'OK' if sgn_ok else 'FAIL'}")

    # --- extreme-outlier detection: AGNOSTIC (value / fraction / sign independent) ---
    bulk = np.linspace(0, 700, 800)
    # legacy case preserved: 2x 0x0FFF (12-bit all-ones), clean -> None, signed -> None
    sat = np.concatenate([bulk, [4095.0, 4095.0]])
    info = detect_extreme_outliers(sat, 16)
    clean = detect_extreme_outliers(bulk, 16)
    signed_ok = detect_extreme_outliers(np.linspace(-200, 200, 500), 16) is None
    legacy_ok = (info is not None and info["count"] == 2 and info["looks_maxed"]
                 and info["kind"] == "sentinel" and info["confidence"] == "high"
                 and clean is None and signed_ok)
    # (a) fraction > 2% (the old 2% cap would have bailed)
    a = detect_extreme_outliers(np.concatenate([bulk, np.full(40, 4095.0)]), 16)
    case_a = a is not None and a["confidence"] == "high" and a["frac"] > 0.02
    # (b) multiple distinct, structural, 16-bit read (e.g. 0x3FFF/0x4000/0x4041)
    rpm = np.linspace(600, 2300, 800)
    trip = np.tile([16383.0, 16384.0, 16449.0], 15)
    b = detect_extreme_outliers(np.concatenate([rpm, trip]), 16)
    case_b = (b is not None and len(b["u_values"]) >= 2 and b["near_structural"]
              and b["confidence"] == "high")
    # (c) non-all-ones FAR constant: flagged by gap+teleport, NOT by the bit pattern
    c = detect_extreme_outliers(np.concatenate([bulk, np.full(30, 5000.0)]), 16)
    case_c = c is not None and not c["near_structural"]
    # (d) ramp to a high value with no gap/teleport -> NOT flagged
    case_d = detect_extreme_outliers(np.linspace(0, 4095, 800), 16) is None
    # (e) full-range continuous signal -> NOT flagged (core anti-overfit assertion)
    case_e = detect_extreme_outliers(np.linspace(0, 65535, 2000), 16) is None
    # (f) signed most-negative sentinel: detected on the LOW side
    f = detect_extreme_outliers(
        np.concatenate([np.linspace(-50, 50, 500), np.full(25, -32768.0)]), 16)
    case_f = (f is not None and f["gap_mad_lo"] > 0 and f["near_structural"]
              and f["confidence"] == "high")
    # (g) sparse: below min_n -> None; just above with a cluster -> sane, not "high"
    case_g1 = detect_extreme_outliers(np.linspace(0, 100, 15), 16) is None
    g2 = detect_extreme_outliers(
        np.concatenate([np.linspace(0, 100, 22), [9000.0, 9000.0]]), 16)
    case_g2 = g2 is None or g2["confidence"] != "high"
    # (h) single isolated spike -> "outlier" (user decides), not a structural sentinel
    spike = bulk.copy(); spike[400] = 9000.0
    h = detect_extreme_outliers(spike, 16)
    case_h = h is not None and h["kind"] == "outlier"
    flags = (legacy_ok, case_a, case_b, case_c, case_d, case_e, case_f,
             case_g1, case_g2, case_h)
    eo_ok = all(flags)
    ok = ok and eo_ok
    print(f"  extreme-outlier agnostic (legacy/a-h "
          f"{''.join(str(int(x)) for x in flags)}) -> {'OK' if eo_ok else 'FAIL'}")

    # --- auto-round gated on systematic bias, not R^2 ---
    rawv = np.arange(0, 700, 2, dtype=np.float64)
    # (a) true-0.1 signal, integer-quantized ref: fit ~0.1 -> snap cleanly (auto)
    refA = np.round(0.1 * rawv)
    sA, oA = np.polyfit(rawv, refA, 1)
    prA = propose_round_calibration(sA, oA, rawv, refA)
    okA = (prA is not None and abs(prA["scale"] - 0.1) < 1e-9 and prA["auto"]
           and abs(prA["offset"]) < 1e-9)
    # (b) signal 2.4% off 0.1 (indicated-vs-true): snap-to-0.1 is FLAGGED, not auto
    refB = 0.0976 * rawv
    sB, oB = np.polyfit(rawv, refB, 1)
    prB = propose_round_calibration(sB, oB, rawv, refB)
    okB = (prB is not None and abs(prB["scale"] - 0.1) < 1e-9
           and not prB["auto"] and prB["bias_frac"] > 0.01)
    # (c) non-nice scale, zero offset: nothing to round
    okC = propose_round_calibration(1.0 / 2593.0, 0.0, rawv, refA) is None
    ar_ok = okA and okB and okC
    ok = ok and ar_ok
    print(f"  auto-round bias-gated (clean-auto/biased-flag/none) -> "
          f"{'OK' if ar_ok else 'FAIL'}")

    # --- decimal tidy-up: fewest decimals without losing significant precision ---
    rawD = np.arange(0, 18000, 1, dtype=np.float64)   # raw span ~18000 (like a speed field)
    # (a) a long float tail is dropped to a handful of decimals within the budget
    drA = propose_decimal_round(0.123456, 0.0, np.arange(0, 100, 1, dtype=np.float64))
    okA = (drA is not None and drA["scale_decimals"] == 4
           and abs(drA["scale"] - 0.1235) < 1e-12 and drA["bias_frac"] <= ROUND_DECIMALS_TOL)
    # (b) a coarse signal where 3dp is enough -> 0.001 (don't keep needless digits)
    drB = propose_decimal_round(0.0010004, 0.0, np.arange(0, 200, 1, dtype=np.float64))
    okB = (drB is not None and abs(drB["scale"] - 0.001) < 1e-12
           and drB["scale_decimals"] == 3)
    # (c) a fine signal that genuinely NEEDS more decimals keeps them (no over-round
    # to a coarse 0.0001) - and the rounding stays within the precision budget
    drC = propose_decimal_round(0.00010709, 0.0, rawD)
    okC = (drC is not None and drC["scale_decimals"] >= 6
           and drC["bias_frac"] <= ROUND_DECIMALS_TOL)
    # (d) an already-short value -> nothing to change
    okD = propose_decimal_round(0.5, 0.0, rawD) is None
    dr_ok = okA and okB and okC and okD
    ok = ok and dr_ok
    print(f"  decimal tidy-up (drop-tail/coarse-3dp/keep-fine/noop) -> "
          f"{'OK' if dr_ok else 'FAIL'}")

    # --- physical-anchor re-fit: honour a known rest/zero state ---
    # speed-like signal, true (0.1, 0): a fit that absorbed a low-end bias into a
    # negative offset (decodes <0 at rest) must be re-fit through the dense rest
    # cluster - recovering BOTH the true scale (0.1) and offset (0), not just an
    # offset shift (auto, since the anchor disagreement is small).
    rawS = np.concatenate([np.zeros(120), np.arange(1, 700, 3, dtype=np.float64)])
    refS = 0.1 * rawS
    acA = propose_anchor_calibration(0.0983, -1.57, rawS, refS)   # fit drifted negative
    okA = (acA is not None and acA["auto"] and acA["delta"] > 0
           and abs(acA["scale"] - 0.1) < 1e-6 and abs(acA["offset"]) < 1e-6
           and abs(acA["scale"] * acA["raw_rest"] + acA["offset"]) < 1e-6)
    # a CORRECT fit (rest already decodes ~0) -> nothing to do
    okB = propose_anchor_calibration(0.1, 0.0, rawS, refS) is None
    # a non-zero rest (idle ~800) is NOT auto-anchored without an explicit value
    rawR = np.concatenate([np.full(120, 80.0), np.arange(80, 700, 3, dtype=np.float64)])
    refR = 10.0 * rawR
    okC = propose_anchor_calibration(10.0, 0.0, rawR, refR) is None
    # explicit anchor on that non-zero rest: re-fit so the rest state decodes 800
    acD = propose_anchor_calibration(9.7, 200.0, rawR, refR, anchor_value=800.0)
    okD = acD is not None and abs((acD["scale"] * acD["raw_rest"] + acD["offset"]) - 800.0) < 1e-6
    # a LARGE disagreement at the anchor -> NOT auto (field likely wrong)
    acE = propose_anchor_calibration(0.1, -30.0, rawS, refS)
    okE = acE is not None and not acE["auto"]
    anc_ok = okA and okB and okC and okD and okE
    ok = ok and anc_ok
    print(f"  physical-anchor re-fit (zero-auto/honoured/nonzero-skip/forced/large-flag)"
          f" -> {'OK' if anc_ok else 'FAIL'}")

    # --- theoretical_range: representable range from bits + scale/offset ---
    tr_u = theoretical_range(16, False, 0.1, 0.0)        # 0 .. 6553.5
    tr_s = theoretical_range(16, True, 0.1, 0.0)         # -3276.8 .. 3276.7
    tr_neg = theoretical_range(8, False, -1.0, 10.0)     # scale<0 flips: -245 .. 10
    tr_ok = (abs(tr_u[0]) < 1e-9 and abs(tr_u[1] - 6553.5) < 1e-6
             and abs(tr_s[0] + 3276.8) < 1e-6 and abs(tr_s[1] - 3276.7) < 1e-6
             and abs(tr_neg[0] + 245.0) < 1e-9 and abs(tr_neg[1] - 10.0) < 1e-9)
    ok = ok and tr_ok
    print(f"  theoretical_range unsigned/signed/neg-scale -> "
          f"{'OK' if tr_ok else 'FAIL'}")

    # --- resolution refinement: recover true field width from TRANSITION data ---
    def _piecewise(levels, hold_s=3.0, ramp_s=1.0, fs=1000, ramp=True):
        kt, kv, tt = [], [], 0.0
        for i, lv in enumerate(levels):
            kt += [tt, tt + hold_s]; kv += [lv, lv]; tt += hold_s
            if i < len(levels) - 1:
                step = ramp_s if ramp else 1e-3
                kt += [tt, tt + step]; kv += [lv, levels[i + 1]]; tt += ramp_s
        tt_arr = np.arange(0, tt, 1.0 / fs)
        return tt_arr, np.interp(tt_arr, kt, kv)

    L16 = [5208, 4040, 2536, 1552, 8, 1552, 2536, 4040, 5208]   # low 3 bits 0 (gauge-like)
    # (1) a real 16-bit field sampled with holds + ramps: must grow well past 5 bits
    t1, w1 = _piecewise(L16)
    w1 = np.clip(np.round(w1 / 8) * 8, 0, 65535).astype(np.int64)
    r1 = refine_field_resolution(w1, t1, "little", 8, 5, False)
    okR1 = r1[1] >= 10 and r1[0] <= 4
    # (2) a genuine 5-bit field at bits 8-12 with a NOISE neighbour in bits 0-7:
    #     must NOT grow into the noise (smoothness gate + cascade floor both stop it)
    L5 = [20, 15, 9, 6, 0, 6, 9, 15, 20]
    t2, w5 = _piecewise(L5)
    w5 = np.clip(np.round(w5), 0, 31).astype(np.int64)
    noise = np.random.default_rng(0).integers(0, 256, size=w5.shape)
    r2 = refine_field_resolution((w5 << 8) | noise, t2, "little", 8, 5, False)
    okR2 = r2 == (8, 5)
    # (3) instant steps (no ramps): the flip-rate CASCADE still recovers the width
    #     (it only needs bit toggling, not smooth ramps) - the dither-tolerant fix.
    t3, w3 = _piecewise(L16, ramp=False)
    w3 = np.clip(np.round(w3 / 8) * 8, 0, 65535).astype(np.int64)
    r3 = refine_field_resolution(w3, t3, "little", 8, 5, False)
    okR3 = r3[0] <= 4 and r3[1] >= 10
    # (4) a rolling COUNTER below the field must NOT be swallowed: the flip-rate
    #     drops/jumps at the boundary, so the cascade stops at the field's true LSB.
    t4, f4 = _piecewise([10, 120, 200, 90, 10])
    f4 = np.clip(np.round(f4), 0, 255).astype(np.int64)
    ctr4 = (np.arange(len(t4)) % 256).astype(np.int64)
    le4 = np.array([(int(a) << 8) | int(b) for a, b in zip(f4, ctr4)], dtype=object)
    r4 = refine_field_resolution(le4, t4, "little", 8, 8, False)
    okR4 = r4[0] == 8
    res_ok = okR1 and okR2 and okR3 and okR4
    ok = ok and res_ok
    print(f"  resolution refine (ramp-grows {r1}/noise-stops {r2}/no-ramp-cascade {r3}/"
          f"counter-stops {r4}) -> {'OK' if res_ok else 'FAIL'}")

    # --- byte-alignment snap: extend across CONSTANT bits to a standard width ---
    chg = np.zeros(64, bool); chg[3:13] = True               # exercised bits 3..12
    snap = snap_to_boundary(3, 10, chg, signed=False)
    okS1 = (snap is not None and snap["aligned"] == (0, 16) and snap["scale_factor"] == 8
            and snap["constant_below"] == [0, 1, 2]
            and snap["constant_above"] == [13, 14, 15] and snap["lsb_moved"]
            and snap["msb_moved"])
    chg2 = np.zeros(64, bool); chg2[0:16] = True              # already 16-bit aligned
    okS2 = snap_to_boundary(0, 16, chg2, signed=False) is None
    chg3 = np.zeros(64, bool); chg3[2:13] = True              # bit 2 active blocks LSB snap
    s3 = snap_to_boundary(3, 10, chg3, signed=False)
    okS3 = s3 is None or s3["aligned"][0] >= 3
    chg4 = np.zeros(64, bool); chg4[0:13] = True              # signed: don't move the sign MSB
    s4 = snap_to_boundary(0, 13, chg4, signed=True)
    okS4 = s4 is None or not s4["msb_moved"]
    snap_ok = okS1 and okS2 and okS3 and okS4
    ok = ok and snap_ok
    print(f"  byte-align snap (3|10->0|16 {okS1}/aligned-none {okS2}/active-block {okS3}/"
          f"signed-msb {okS4}) -> {'OK' if snap_ok else 'FAIL'}")

    # --- headless plot smoke test: all four step visualizations render to PNG ---
    plot_ok = _plot_smoke()
    ok = ok and plot_ok
    print(f"  plot smoke (bus/correlation/bitsearch/fit/resolution-refine/cascade PNGs) -> "
          f"{'OK' if plot_ok else 'FAIL'}")

    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _plot_smoke() -> bool:
    """Render each analysis visualization from tiny synthetic inputs (Agg, no
    hardware/data) and assert each PNG exists and is non-empty."""
    import shutil
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp(prefix="re_plots_"))
    try:
        rows = [
            {"id": 0x100, "id_hex": "100", "length": 4, "counter_bytes": [3],
             "checksum_bytes": [], "flag_bits": [0],
             "field_map": [{"start_bit": 1, "length": 10}]},
            {"id": 0x200, "id_hex": "200", "length": 8, "counter_bytes": [],
             "checksum_bytes": [7], "flag_bits": [], "field_map": []},
        ]
        rates = {0x100: np.linspace(0, 1, 32), 0x200: np.linspace(1, 0, 64)}
        p1 = d / "bus.png"
        plot_bus_activity(p1, rows, rates)

        cres = [{"id": 0x100, "id_hex": "100", "byte": b, "width": 2, "order": "big",
                 "signed": False, "score": round(0.2 + 0.15 * b, 3), "plaus": 0.9,
                 "corr_sign": 1, "lag_s": 0.0, "coverage": 1.0, "n": 100}
                for b in range(4)]
        p2 = d / "corr.png"
        plot_correlation_heatmap(p2, cres, "continuous", winner=cres[-1])

        bres = [{"order": "little", "start_bit": s, "length": L, "byte": s // 8,
                 "width": 2, "r2": round(min(1.0, 0.4 + 0.04 * L), 4)}
                for s in range(0, 16) for L in range(4, 17)]
        bres += [{"order": "big", "byte": b, "width": w, "length": w * 8,
                  "start_bit": b * 8 + 7, "r2": round(min(1.0, 0.5 + 0.12 * w), 4)}
                 for b in range(4) for w in range(1, 4)]
        win = {"order": "big", "byte": 0, "width": 2, "length": 16, "r2": 0.999,
               "start_bit": 7}
        p3 = d / "grid.png"
        plot_bitsearch_grid(p3, bres, 0x19F, winner=win)

        raw = np.arange(0, 700, 5, dtype=np.float64)
        ref = 0.0976 * raw + np.random.default_rng(0).normal(0, 0.5, raw.size)
        rc = {"scale": 0.1, "offset": 0.0, "bias_frac": 0.025, "auto": False,
              "scale_changed": True, "offset_changed": True}
        p4 = d / "fit.png"
        plot_fit_diagnostic(p4, raw, ref, 0.0976, 0.0, name="speed", unit="km/h",
                            round_candidate=rc, r2=0.99)

        # resolution-refine: a narrow staircase vs a smooth ramp over a transition
        tt = np.arange(0, 6, 0.001)
        word = np.interp(tt, [0, 2, 3, 6], [5208, 5208, 8, 8])   # hold -> ramp -> hold
        word = (np.round(word / 8) * 8).astype(np.int64)
        p5 = d / "refine.png"
        plot_resolution_refine(p5, tt, ((word >> 8) & 0x1F).astype(float),
                               ((word >> 3) & 0x3FF).astype(float),
                               narrow_label="8|5", refined_label="3|10")

        # bit-cascade: a synthetic ~2x cascade over bits 3..12, constant elsewhere
        crate = np.zeros(64)
        for k in range(3, 13):
            crate[k] = 0.08 / (2 ** (k - 3))
        p6 = d / "cascade.png"
        plot_bit_cascade(p6, crate, can_id=0x2, start_bit=3, length=10,
                         aligned=(0, 16))
        return all(p.exists() and p.stat().st_size > 0
                   for p in (p1, p2, p3, p4, p5, p6))
    except Exception as exc:  # noqa: BLE001 - surfaced as a selftest FAIL
        print(f"  plot smoke EXCEPTION: {exc}")
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(__doc__)
