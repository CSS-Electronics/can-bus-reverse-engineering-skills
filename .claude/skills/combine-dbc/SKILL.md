---
name: combine-dbc
description: Combine multiple individual single-signal DBC files into one combined DBC at the application level. Use whenever the user wants to merge/combine/assemble several per-signal .dbc files (e.g. produced by the cansub-reverse-engineering skill under decoding-output/<application>/<signal>/<signal>.dbc) into a single application-wide DBC such as decoding-output/<application>/<application>.dbc. Re-runnable at any time as more signals are decoded. Triggers on phrasings like "combine the DBCs", "merge my signal DBCs", "build the combined/application DBC", "make one DBC from these".
---

# combine-dbc

Combine the individual single-signal DBC files for one application into a single
combined DBC, written at the application level. Built to pair with the
`cansub-reverse-engineering` skill's output structure, and safe to re-run at any
time as new signals are confirmed.

## Output structure it works with

```
decoding-output/
  <application>/                      e.g. sensor-to-can/
    <signal>/<signal>.dbc             e.g. gauge1/gauge1.dbc   (inputs, one per signal)
    <application>.dbc                 e.g. sensor-to-can.dbc   (combined output)
```

DBC **filenames** are lowercase **kebab-case**; DBC *signal/message* identifiers
inside the files use the plain/underscored form (hyphens aren't valid there).

## Environment — run from the project venv

This script needs `cantools`, installed in the repo's project-local virtual
environment at `.venv/` (built from `requirements.txt`). **In the commands below,
`python` means the venv interpreter** — `.venv\Scripts\python.exe` (Windows) or
`.venv/bin/python` (macOS / Linux), not the system Python. If `.venv/` does not
exist yet, **stop and ask the user to run the one-time setup**: `install.bat`
(Windows) or `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
(macOS / Linux).

## Usage

Combine every confirmed signal for an application (auto-scans `*/*.dbc`, one
folder level down, which excludes the combined file itself):

```
python scripts/combine_dbc.py --app sensor-to-can
# -> decoding-output/sensor-to-can/sensor-to-can.dbc
```

Options:

- `--app <name>` — application folder under `--base` (default base
  `decoding-output`). Output defaults to `<app-dir>/<app>.dbc`.
- `--app-dir <path>` — point at an application folder directly (alternative to
  `--app`/`--base`).
- `--inputs a/a.dbc b/b.dbc` — combine an explicit list (relative to the app dir)
  instead of auto-scanning.
- `--out <path>` — override the combined-DBC output path.

## Behaviour

- Signals sharing a CAN **frame id** are merged into one message.
- A re-merged signal with the **same name replaces** the previous definition, so
  re-running after re-decoding a signal is idempotent (no duplicates).
- The combined `<application>.dbc` at the app-dir root is never fed back into
  itself (it's excluded from the `*/*.dbc` scan).
- Prints the inputs it combined and a per-message signal summary.

## Notes

- Requires `cantools` (already a dependency of the reverse-engineering toolchain).
- This is the structured, all-at-once combine. The reverse-engineering skill also
  ships a lower-level `merge_dbc.py` that folds in **one** DBC at a time; prefer
  `combine-dbc` for assembling the whole application DBC.
