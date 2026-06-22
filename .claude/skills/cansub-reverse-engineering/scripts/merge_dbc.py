"""
merge_dbc.py - accumulate confirmed signals into a growing project DBC.

Merges a single-signal DBC (from build_dbc.py) into a project DBC, creating the
project file if needed. Signals on the same frame id are combined into one
message; a re-merged signal of the same name replaces the old definition.

Example:
    python merge_dbc.py --into temp-output/project.dbc --add temp-output/VehicleSpeed.dbc
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cantools


def _rebuild_message(frame_id, name, is_ext, signals):
    length = max((s.start + s.length + 7) // 8 for s in signals)
    return cantools.database.can.Message(
        frame_id=frame_id, name=name, length=length,
        is_extended_frame=is_ext, signals=signals)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--into", required=True, help="project DBC (created if missing)")
    ap.add_argument("--add", required=True, help="single-signal DBC to merge in")
    args = ap.parse_args()

    into = Path(args.into)
    project = cantools.database.load_file(str(into)) if into.exists() \
        else cantools.database.Database()
    add = cantools.database.load_file(args.add)

    by_id = {m.frame_id: m for m in project.messages}
    added, updated = 0, 0
    for m in add.messages:
        if m.frame_id in by_id:
            existing = by_id[m.frame_id]
            names = {s.name for s in m.signals}
            kept = [s for s in existing.signals if s.name not in names]
            merged = kept + list(m.signals)
            by_id[m.frame_id] = _rebuild_message(
                m.frame_id, existing.name, existing.is_extended_frame, merged)
            updated += 1
        else:
            by_id[m.frame_id] = m
            added += 1

    out_db = cantools.database.Database(messages=list(by_id.values()))
    into.parent.mkdir(parents=True, exist_ok=True)
    # newline="" preserves cantools' CRLF line endings verbatim; without it,
    # text-mode writing on Windows translates each \n to \r\n, yielding \r\r\n.
    into.write_text(out_db.as_dbc_string(), encoding="utf-8", newline="")
    total_sigs = sum(len(m.signals) for m in out_db.messages)
    print(f"Merged into {into}: {added} new message(s), {updated} updated; "
          f"{len(out_db.messages)} messages / {total_sigs} signals total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
