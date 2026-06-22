"""
annotate.py - log a human reference timeline (sidecar) for correlation.

Writes a sidecar CSV (epoch;kind;label;value) timestamped with host epoch time,
which aligns with the trace because the CANsub sets its clock to host time on
connect. Passive - writes a file only, nothing on the bus.

Modes:
  discrete   - mark events (door lock/unlock, button) with a keypress.
               SPACE or ENTER = mark event;  q = quit.
  continuous - drive a 0..100 (or --min..--max) value in sync with reality,
               e.g. a dashboard reading or an encoder you rotate by hand.
               UP/DOWN = +/-1 step,  PGUP/PGDN = +/-10 step,  q = quit.

Examples:
    python annotate.py --mode discrete   --label run
    python annotate.py --mode continuous --label run --unit km/h --min 0 --max 150
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

IS_WIN = sys.platform == "win32"
if IS_WIN:
    import msvcrt


def now() -> float:
    return time.time()


class Sidecar:
    def __init__(self, path: Path, label: str):
        self.label = label
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "w", encoding="utf-8", buffering=1)  # line-buffered
        self.f.write("epoch;kind;label;value\n")
        self.path = path
        self._lock = threading.Lock()

    def write(self, kind: str, value="") -> None:
        with self._lock:
            self.f.write(f"{now():.6f};{kind};{self.label};{value}\n")

    def close(self):
        with self._lock:
            self.f.close()


def _read_key():
    """Blocking single-key read; returns a normalized token or the char."""
    if IS_WIN:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):       # special key prefix
            ch2 = msvcrt.getwch()
            return {"H": "UP", "P": "DOWN", "I": "PGUP", "Q": "PGDN"}.get(ch2, "")
        if ch in ("\r", "\n"):
            return "ENTER"
        if ch == " ":
            return "SPACE"
        return ch.lower()
    # POSIX fallback: read lines (no raw arrow keys)
    line = sys.stdin.readline()
    return "ENTER" if line.strip() == "" else line.strip().lower()


def run_discrete(sc: Sidecar) -> int:
    print("DISCRETE mode. SPACE/ENTER = mark event, q = quit.")
    sc.write("start")
    n = 0
    while True:
        k = _read_key()
        if k == "q":
            break
        if k in ("SPACE", "ENTER"):
            sc.write("event", 1)
            n += 1
            print(f"  event #{n} @ {time.strftime('%H:%M:%S')}")
    sc.write("stop")
    print(f"Logged {n} events -> {sc.path}")
    return 0


def run_continuous(sc: Sidecar, vmin: float, vmax: float, unit: str,
                   step: float, start: float, heartbeat: float = 0.25) -> int:
    print(f"CONTINUOUS mode [{vmin}..{vmax} {unit}]. "
          f"UP/DOWN=+/-{step}, PGUP/PGDN=+/-{step*10}, q=quit.")
    if not IS_WIN:
        print("  (non-Windows: type a number + ENTER to set value, blank=quit)")
    if heartbeat > 0:
        print(f"  (parked value re-logged every {heartbeat:g}s so holds count too)")
    state = {"val": start, "running": True}
    sc.write("value", f"{start:.3f}")
    print(f"  value = {start:.2f} {unit}")

    def _heartbeat():
        while state["running"]:
            time.sleep(heartbeat)
            if state["running"]:
                sc.write("value", f"{state['val']:.3f}")
    hb = None
    if heartbeat > 0:
        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()

    val = start
    while True:
        if IS_WIN:
            k = _read_key()
            if k == "q":
                break
            elif k == "UP":
                val += step
            elif k == "DOWN":
                val -= step
            elif k == "PGUP":
                val += step * 10
            elif k == "PGDN":
                val -= step * 10
            else:
                continue
        else:
            line = sys.stdin.readline()
            if line.strip() == "":
                break
            try:
                val = float(line.strip())
            except ValueError:
                continue
        val = max(vmin, min(vmax, val))
        state["val"] = val
        sc.write("value", f"{val:.3f}")
        print(f"  value = {val:6.2f} {unit}")
    state["running"] = False
    sc.write("stop")
    print(f"Logged value ramp -> {sc.path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["discrete", "continuous"], required=True)
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", help="sidecar CSV (default temp-output/sidecar_<label>.csv)")
    ap.add_argument("--unit", default="%")
    ap.add_argument("--min", dest="vmin", type=float, default=0.0)
    ap.add_argument("--max", dest="vmax", type=float, default=100.0)
    ap.add_argument("--step-pct", type=float, default=1.0,
                    help="UP/DOWN increment as %% of range (PGUP/PGDN = 10x)")
    ap.add_argument("--start", type=float, help="initial value (default = min)")
    ap.add_argument("--heartbeat", type=float, default=0.25,
                    help="continuous: re-log the current value every N s so parked "
                         "holds are densely sampled (0 disables)")
    args = ap.parse_args()

    out = Path(args.out) if args.out else Path(f"temp-output/sidecar_{args.label}.csv")
    sc = Sidecar(out, args.label)
    try:
        if args.mode == "discrete":
            return run_discrete(sc)
        step_units = args.step_pct / 100.0 * (args.vmax - args.vmin)
        start = args.start if args.start is not None else args.vmin
        return run_continuous(sc, args.vmin, args.vmax, args.unit, step_units, start,
                              heartbeat=args.heartbeat)
    except KeyboardInterrupt:
        sc.write("stop")
        print("\nInterrupted.")
        return 0
    finally:
        sc.close()


if __name__ == "__main__":
    raise SystemExit(main())
