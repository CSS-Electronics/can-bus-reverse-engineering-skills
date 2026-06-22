"""
vision_review.py - visually confirm a vision-OCR reference against the source video.

Companion to vision_reference.py. Opens a small local web app that plays the VIDEO
(top-left) next to a big STAT readout of the current digitized value (top-right), over a
full-range uPlot of the digitized reference series (bottom) with a PLAYHEAD that tracks
the video. Play/Reset buttons, and the native video scrubber keeps the stat + playhead in
sync as you seek - so you can eyeball that the OCR'd reference matches the on-screen number
BEFORE it feeds the decoder (OCR can misread).

Reuses the flask_sync.py scaffolding (Flask app + port check + launch) and the vendored,
offline uPlot in ../assets, and common.load_sidecar() for the CSV.

    python scripts/vision_review.py --video TEMP/clip.mov --sidecar temp-output/sidecar_speed_ref.csv
    # reads temp-output/sidecar_speed_ref.meta.json for the video<->sidecar time mapping
    # open http://127.0.0.1:5001
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template_string, send_file

import common  # noqa: F401

ASSETS = str(Path(__file__).resolve().parent.parent / "assets")

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Vision reference review</title>
<link rel="stylesheet" href="/static/uPlot.min.css">
<style>
  :root { --blue:#3d85c6; --orange:#ff9900; --green:#10cba9; --red:#ff6666; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, sans-serif; background:#fff; color:#1a1d21; }
  .apptitle { font-size:1.15rem; font-weight:700; padding:12px 16px 0; background:#f7f8fa; }
  .sub { font-size:.8rem; color:#5b6573; padding:2px 16px 10px; background:#f7f8fa;
         border-bottom:1px solid #e2e5ea; }
  .top { display:flex; gap:16px; padding:14px 16px 6px; align-items:stretch; }
  .vidbox { flex:1 1 55%; min-width:0; }
  video { width:100%; max-height:52vh; background:#000; border:1px solid #e2e5ea;
          border-radius:12px; }
  .statbox { flex:1 1 45%; display:flex; flex-direction:column; justify-content:center;
             align-items:center; border:1px solid #e2e5ea; border-radius:12px;
             background:#f7f8fa; padding:10px; }
  .statval { font-size:5.5rem; font-weight:800; color:var(--blue); line-height:1;
             font-variant-numeric:tabular-nums; }
  .statunit { font-size:1.4rem; color:#5b6573; font-weight:600; margin-top:6px; }
  .stattime { font-size:.95rem; color:#9aa3af; margin-top:14px; font-variant-numeric:tabular-nums; }
  .controls { display:flex; gap:10px; padding:6px 16px 4px; align-items:center; }
  .controls button { font-size:.95rem; font-weight:700; color:#fff; border:none;
            border-radius:9px; padding:9px 18px; cursor:pointer; }
  #play { background:var(--blue); } #reset { background:#5b6573; }
  .controls button:hover { filter:brightness(1.07); }
  .bottom { padding:6px 16px 14px; }
  #plot { background:#fff; border:1px solid #e2e5ea; border-radius:12px; padding:8px; }
</style></head><body>
<div class="apptitle">Vision reference review</div>
<div class="sub" id="sub"></div>
<div class="top">
  <div class="vidbox"><video id="vid" src="/video" preload="auto" controls playsinline></video></div>
  <div class="statbox">
    <div class="statval" id="sval">--</div>
    <div class="statunit" id="sunit"></div>
    <div class="stattime" id="stime">t = 0.00 s</div>
  </div>
</div>
<div class="controls">
  <button id="play">&#9654; Play</button>
  <button id="reset">&#8634; Reset</button>
</div>
<div class="bottom"><div id="plot"></div></div>
<script src="/static/uPlot.iife.min.js"></script>
<script>
const vid = document.getElementById('vid');
let T = [], V = [], unit = '', label = '', vmin = 0, vmax = 1, curT = 0;

const fmt = x => (x === null || x === undefined) ? '--'
  : (Math.abs(x) >= 100 ? x.toFixed(0) : Math.abs(x) >= 10 ? x.toFixed(1) : x.toFixed(2));

// nearest sample at or before video time t (binary search on T, sorted ascending)
function valueAt(t){
  if (!T.length) return null;
  let lo = 0, hi = T.length - 1;
  if (t <= T[0]) return V[0];
  if (t >= T[hi]) return V[hi];
  while (lo < hi){ const mid = (lo + hi + 1) >> 1; if (T[mid] <= t) lo = mid; else hi = mid - 1; }
  return V[lo];
}

// playhead: a vertical line at the current video time
function playhead(){
  return { hooks: { draw: u => {
    if (curT < u.scales.x.min || curT > u.scales.x.max) return;
    const x = u.valToPos(curT, 'x', true);
    const ctx = u.ctx; ctx.save();
    ctx.strokeStyle = '#ff9900'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(x, u.bbox.top); ctx.lineTo(x, u.bbox.top + u.bbox.height);
    ctx.stroke(); ctx.restore();
  }}};
}

let u = null;
function buildPlot(){
  const pad = (vmax - vmin) * 0.05 || 1;
  const opts = {
    width: document.getElementById('plot').clientWidth - 16,
    height: 0.34 * window.innerHeight,
    scales: { x: { time:false }, y: { range: () => [vmin - pad, vmax + pad] } },
    series: [ {label:'t (s)'},
              {label: label || 'value', stroke:'#3d85c6', width:2,
               paths: uPlot.paths.stepped({align:1})} ],
    axes: [ {stroke:'#888', grid:{stroke:'#e6e8ec'}}, {stroke:'#888', grid:{stroke:'#e6e8ec'}} ],
    plugins: [ playhead() ],
  };
  u = new uPlot(opts, [T, V], document.getElementById('plot'));
  window.addEventListener('resize', () => u.setSize(
    {width: document.getElementById('plot').clientWidth - 16, height: 0.34*window.innerHeight}));
}

function refresh(){
  curT = vid.currentTime || 0;
  const v = valueAt(curT);
  document.getElementById('sval').textContent = fmt(v);
  document.getElementById('stime').textContent = 't = ' + curT.toFixed(2) + ' s';
  if (u) u.redraw(false, false);
}
function loop(){ refresh(); if (!vid.paused && !vid.ended) requestAnimationFrame(loop); }

vid.addEventListener('play', () => requestAnimationFrame(loop));
vid.addEventListener('timeupdate', refresh);
vid.addEventListener('seeking', refresh);
vid.addEventListener('seeked', refresh);
document.getElementById('play').onclick = () => { vid.paused ? vid.play() : vid.pause(); };
document.getElementById('reset').onclick = () => { vid.pause(); vid.currentTime = 0; refresh(); };

fetch('/api/series').then(r => r.json()).then(d => {
  T = d.t; V = d.v; unit = d.unit; label = d.label; vmin = d.vmin; vmax = d.vmax;
  document.getElementById('sunit').textContent = unit;
  document.getElementById('sub').textContent =
    `${d.n} samples · ${d.label} [${unit}] · ${d.rate.toFixed(1)} Hz · range ${fmt(vmin)}–${fmt(vmax)} · video ${d.video}`;
  buildPlot(); refresh();
});
</script></body></html>"""


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", required=True, help="the source video")
    ap.add_argument("--sidecar", required=True, help="vision sidecar CSV to review")
    ap.add_argument("--meta", help="meta JSON (default: alongside the sidecar)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5001)
    args = ap.parse_args()

    video = Path(args.video)
    sidecar = Path(args.sidecar)
    if not video.is_file():
        print(f"ERROR: video not found: {video}", file=sys.stderr)
        return 1
    if not sidecar.is_file():
        print(f"ERROR: sidecar not found: {sidecar}", file=sys.stderr)
        return 1

    # time mapping: video_time = epoch - start_epoch - time_offset  (the actual frame
    # time the value was read at; time_offset is a CAN-sync nudge, irrelevant to review).
    meta_path = Path(args.meta) if args.meta else sidecar.with_name(
        sidecar.stem + ".meta.json")
    start_epoch = time_offset = None
    label, unit = sidecar.stem.replace("sidecar_", ""), ""
    if meta_path.is_file():
        m = json.loads(meta_path.read_text(encoding="utf-8"))
        start_epoch, time_offset = m.get("start_epoch"), m.get("time_offset", 0.0)
        label, unit = m.get("label", label), m.get("unit", "")

    df = common.load_sidecar(str(sidecar))
    rows = df[df["kind"] == "value"].dropna(subset=["value"])
    if rows.empty:
        print("ERROR: no kind=value rows in the sidecar.", file=sys.stderr)
        return 1
    epochs = rows["epoch"].to_numpy(float)
    values = rows["value"].to_numpy(float)
    if start_epoch is None:  # no meta: assume first sample ~ video start
        start_epoch, time_offset = float(epochs[0]), 0.0
        print(f"  (no {meta_path.name}; assuming first sample = video t0)", file=sys.stderr)
    t = epochs - float(start_epoch) - float(time_offset or 0.0)

    span = float(t[-1] - t[0]) if len(t) > 1 else 0.0
    series = {
        "t": [round(float(x), 4) for x in t],
        "v": [float(x) for x in values],
        "unit": unit, "label": label,
        "vmin": float(values.min()), "vmax": float(values.max()),
        "n": int(len(values)), "rate": (len(t) / span if span > 0 else 0.0),
        "video": video.name,
    }

    if _port_in_use(args.host, args.port):
        print(f"[!] {args.host}:{args.port} already in use - relaunch with --port <N>.",
              file=sys.stderr)
        return 2

    app = Flask(__name__, static_folder=ASSETS, static_url_path="/static")

    @app.get("/")
    def index():
        return render_template_string(PAGE)

    @app.get("/video")
    def serve_video():
        # conditional=True -> HTTP range requests, so the <video> scrubber can seek.
        return send_file(str(video.resolve()), conditional=True)

    @app.get("/api/series")
    def api_series():
        return jsonify(series)

    print(f"Review UI on http://{args.host}:{args.port}  "
          f"({series['n']} samples of '{label}' [{unit}] vs {video.name})")
    print("Play/scrub the video; the stat + plot playhead track it. Ctrl-C to stop.")
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
