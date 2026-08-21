#!/usr/bin/env python3
"""Click-through recording app. Open in a browser, follow the instructions.

Typing commands and reading terminal prompts while sitting in a car is a poor
way to run a capture protocol. This serves a single page with one big
instruction at a time, a Start button, a countdown, and boxes for the dashboard
readings. It drives the same Recorder as tools/record.py and writes the same
files, so everything downstream is unchanged.

    uv run python tools/gui.py
    then open http://localhost:8000
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from record import CSV_HEADER, MARKER_TAIL_S, Recorder  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROTO_DIR = os.path.join(ROOT, "protocols")

PROTOCOLS = [
    ("soc_snapshot.yaml", "Quick check", "1 minute. Car on, parked. Read 4 numbers off the dash."),
    ("session_a_stationary.yaml", "Main session", "About 15 minutes. Gears, doors, lights, steering. Car stays parked."),
    ("session_b_driving.yaml", "Driving", "About 10 minutes. Needs a second person to hold the laptop."),
    ("session_c_charging.yaml", "Charging", "Needs a charger. The most valuable one if you can do it."),
]


class Session:
    def __init__(self, proto: str, out: str, backend: str, channels: list[str], bitrate: int):
        import yaml
        raw = yaml.safe_load(open(os.path.join(PROTO_DIR, proto)))
        self.steps = []
        for s in raw["steps"]:
            n = s.get("repeat", 1)
            for r in range(n):
                c = dict(s)
                c["tag"] = s["id"] + (f"_r{r+1}" if n > 1 else "")
                c["rep"] = (r + 1, n)
                self.steps.append(c)
        self.out = out
        os.makedirs(out, exist_ok=True)
        self.idx = 0
        self.phase = "ready"          # ready | recording | ask | done
        self.markers: list = []
        self.truth: list = []
        self.frames = 0
        self.error: str | None = None
        self._t_end = 0.0
        self._lock = threading.Lock()
        self.rec = Recorder(backend, channels, bitrate)
        self._fh = open(os.path.join(out, "can_frames.csv"), "w", newline="")
        self._w = csv.writer(self._fh)
        self._w.writerow(CSV_HEADER)
        self._stop = threading.Event()
        try:
            self.rec.start()
        except Exception as exc:
            self.error = f"Could not open the CAN adapter: {exc}"
            self.phase = "done"
            return
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while not self._stop.is_set():
            try:
                n = self.rec.drain(self._w, deadline=time.monotonic() + 0.3)
                with self._lock:
                    self.frames += n
                    if self.phase == "recording" and time.monotonic() >= self._t_end:
                        self._end_step()
            except Exception as exc:
                self.error = str(exc)
                break

    # --- step machine ------------------------------------------------
    @property
    def step(self):
        return self.steps[self.idx] if self.idx < len(self.steps) else None

    def begin(self):
        with self._lock:
            s = self.step
            if not s or self.phase != "ready":
                return
            self.markers.append((time.time() - self.rec.t0, f"{s['tag']}_start", ""))
            self._t_end = time.monotonic() + float(s.get("duration_s", 10))
            self.phase = "recording"

    def _end_step(self):
        s = self.step
        self.markers.append((time.time() - self.rec.t0, f"{s['tag']}_end", ""))
        self.phase = "ask" if s.get("ground_truth") else "ready"
        if self.phase == "ready":
            self._advance()

    def _advance(self):
        self.idx += 1
        if self.idx >= len(self.steps):
            self.phase = "done"
            self.finish()
        else:
            self.phase = "ready"

    def submit(self, values: dict):
        with self._lock:
            if self.phase != "ask":
                return
            s = self.step
            for k, v in values.items():
                if str(v).strip():
                    self.truth.append((time.time() - self.rec.t0, k, str(v).strip(), "", s["tag"]))
            self._advance()

    def skip(self):
        with self._lock:
            if self.phase in ("ready", "ask"):
                self._advance()

    def finish(self):
        if self._stop.is_set():
            return
        self._stop.set()
        time.sleep(0.4)
        try:
            self.rec.stop()
            self.frames += self.rec.drain(self._w)
            self.frames += self.rec.flush_all(self._w)
        except Exception:
            pass
        self._fh.close()
        dur = time.time() - self.rec.t0
        with open(os.path.join(self.out, "markers.csv"), "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["t_s", "label", "note"])
            for t, l, n in self.markers:
                w.writerow([f"{t:.6f}", l, n])
        with open(os.path.join(self.out, "ground_truth.csv"), "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["t_s", "key", "value", "unit", "step"])
            for row in self.truth:
                w.writerow([f"{row[0]:.6f}"] + list(row[1:]))
        late = [m for m in self.markers if m[1].endswith("_start") and dur - m[0] < MARKER_TAIL_S]
        json.dump({
            "backend": self.rec.backend, "channels": self.rec.channels,
            "duration_s": round(dur, 3), "total_frames": self.frames,
            "marker_count": len(self.markers), "ground_truth_count": len(self.truth),
            "gaps": [[round(a, 3), round(b, 3)] for a, b in self.rec.gaps],
            "markers_too_late": [m[1] for m in late],
            "listen_only": False,
            "listen_only_note": "CANalyst-II cannot be set listen-only via python-can; "
                                "it ACKs at hardware level. This tool never transmits.",
        }, open(os.path.join(self.out, "session_meta.json"), "w"), indent=2)

    def status(self) -> dict:
        s = self.step
        remain = max(0.0, self._t_end - time.monotonic()) if self.phase == "recording" else 0.0
        return {
            "phase": self.phase, "error": self.error,
            "idx": self.idx, "total": len(self.steps),
            "tag": s["tag"] if s else None,
            "prompt": s["prompt"] if s else None,
            "rep": s["rep"] if s else None,
            "duration": s.get("duration_s", 10) if s else 0,
            "remain": round(remain, 1),
            "ground_truth": s.get("ground_truth", []) if s else [],
            "frames": self.frames, "out": self.out,
            "markers": len(self.markers), "truth": len(self.truth),
        }


SESSION: Session | None = None
CONFIG: dict = {}

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BluCANRE Recorder</title><style>
:root{--bg:#0f1216;--card:#171c22;--fg:#e8eef5;--mut:#8b98a8;--acc:#3ddc84;--warn:#ffb74d;--err:#ff6b6b;--line:#2a323c}
*{box-sizing:border-box}
body{margin:0;font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
.wrap{max-width:760px;margin:0 auto;padding:24px 18px 60px}
h1{font-size:20px;margin:0 0 4px} .sub{color:var(--mut);font-size:14px;margin-bottom:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;margin-bottom:16px}
.big{font-size:30px;font-weight:650;line-height:1.3;margin:6px 0 18px}
button{font:600 18px system-ui;padding:16px 26px;border:0;border-radius:11px;background:var(--acc);color:#06210f;cursor:pointer;width:100%}
button:active{transform:translateY(1px)} button.ghost{background:#232b34;color:var(--fg);font-size:15px;padding:12px}
button[disabled]{opacity:.45;cursor:default}
.row{display:flex;gap:10px;margin-top:10px}
.opt{background:#1d242c;border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:10px;cursor:pointer}
.opt:hover{border-color:var(--acc)} .opt b{font-size:18px} .opt div{color:var(--mut);font-size:14px;margin-top:4px}
.count{font-size:64px;font-weight:700;text-align:center;color:var(--acc);font-variant-numeric:tabular-nums}
.bar{height:8px;background:#232b34;border-radius:5px;overflow:hidden;margin:16px 0 4px}
.bar>i{display:block;height:100%;background:var(--acc);width:0;transition:width .3s}
label{display:block;margin:14px 0 6px;color:var(--mut);font-size:14px}
input{width:100%;padding:14px;font-size:20px;border-radius:10px;border:1px solid var(--line);background:#0e1318;color:var(--fg)}
.meta{display:flex;gap:20px;color:var(--mut);font-size:13px;margin-top:14px;flex-wrap:wrap}
.pill{background:#232b34;padding:4px 10px;border-radius:20px}
.err{color:var(--err)} .ok{color:var(--acc)} .warn{color:var(--warn)}
</style></head><body><div class="wrap">
<h1>BluCANRE Recorder</h1>
<div class="sub">Tata Tigor EV &middot; reads the car's data bus &middot; never sends anything to the car</div>
<div id="app"></div></div>
<script>
let S=null;
async function api(p,b){const r=await fetch(p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):null});return r.json()}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function tick(){S=await api('/api/status');render();}
function render(){
 const a=document.getElementById('app');
 if(!S.running){
   a.innerHTML='<div class="card"><div style="font-size:18px;margin-bottom:14px">What do you want to record?</div>'+
   S.protocols.map((p,i)=>`<div class="opt" onclick="start(${i})"><b>${esc(p[1])}</b><div>${esc(p[2])}</div></div>`).join('')+
   `<div class="meta"><span class="pill">adapter: ${esc(S.backend)}</span><span class="pill">channels: ${esc(S.channels)}</span></div></div>`;
   return;
 }
 const st=S.status;
 if(st.error){a.innerHTML=`<div class="card"><div class="big err">Problem</div><div>${esc(st.error)}</div>
   <div class="row"><button class="ghost" onclick="location.reload()">Back</button></div></div>`;return;}
 if(st.phase==='done'){
   a.innerHTML=`<div class="card"><div class="big ok">All done</div>
   <div>Saved to <b>${esc(st.out)}</b></div>
   <div class="meta"><span class="pill">${st.frames.toLocaleString()} frames</span>
   <span class="pill">${st.markers} markers</span><span class="pill">${st.truth} dash readings</span></div>
   <div class="row"><button onclick="location.reload()">Record something else</button></div></div>`;return;}
 const pct=Math.round(100*st.idx/st.total);
 let body='';
 if(st.phase==='ready'){
   body=`<div class="sub">Step ${st.idx+1} of ${st.total}${st.rep&&st.rep[1]>1?` &middot; repeat ${st.rep[0]} of ${st.rep[1]}`:''}</div>
   <div class="big">${esc(st.prompt)}</div>
   <button onclick="api('/api/begin',{}).then(tick)">I've done that &mdash; start recording</button>
   <div class="row"><button class="ghost" onclick="api('/api/skip',{}).then(tick)">Skip this step</button></div>`;
 } else if(st.phase==='recording'){
   body=`<div class="sub">Step ${st.idx+1} of ${st.total} &middot; recording</div>
   <div class="big">${esc(st.prompt)}</div>
   <div class="count">${st.remain.toFixed(0)}s</div>
   <div class="sub" style="text-align:center">Hold still, don't touch anything else</div>`;
 } else {
   body=`<div class="sub">Step ${st.idx+1} of ${st.total}</div>
   <div class="big">Now read these off the dashboard</div>`+
   st.ground_truth.map(k=>`<label>${esc(k)}</label><input id="gt_${esc(k)}" autocomplete="off">`).join('')+
   `<div class="row"><button onclick="submit()">Save and continue</button></div>
    <div class="row"><button class="ghost" onclick="api('/api/skip',{}).then(tick)">I can't read these &mdash; skip</button></div>`;
 }
 a.innerHTML=`<div class="card">${body}<div class="bar"><i style="width:${pct}%"></i></div>
  <div class="meta"><span class="pill">${st.frames.toLocaleString()} frames</span>
  <span class="pill">${st.markers} markers</span><span class="pill">${st.truth} readings</span></div></div>
  <div class="card"><button class="ghost" onclick="if(confirm('Stop and save what you have?'))api('/api/finish',{}).then(tick)">Stop and save</button></div>`;
}
async function start(i){await api('/api/start',{index:i});tick();}
async function submit(){const v={};S.status.ground_truth.forEach(k=>{const e=document.getElementById('gt_'+k);if(e)v[k]=e.value});await api('/api/submit',v);tick();}
tick();setInterval(tick,600);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/status"):
            st = {"running": SESSION is not None, "protocols": PROTOCOLS,
                  "backend": CONFIG["backend"], "channels": ",".join(CONFIG["channels"])}
            if SESSION:
                st["status"] = SESSION.status()
            return self._send(200, json.dumps(st))
        return self._send(200, PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        global SESSION
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or "{}")
        if self.path == "/api/start":
            proto = PROTOCOLS[int(data.get("index", 0))][0]
            name = proto.replace(".yaml", "") + time.strftime("_%Y%m%d_%H%M%S")
            SESSION = Session(proto, os.path.join(ROOT, "sessions", name),
                              CONFIG["backend"], CONFIG["channels"], CONFIG["bitrate"])
        elif SESSION and self.path == "/api/begin":
            SESSION.begin()
        elif SESSION and self.path == "/api/submit":
            SESSION.submit(data)
        elif SESSION and self.path == "/api/skip":
            SESSION.skip()
        elif SESSION and self.path == "/api/finish":
            SESSION.phase = "done"
            SESSION.finish()
        return self._send(200, json.dumps({"ok": True}))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default="canalystii", choices=["canalystii", "socketcan"])
    ap.add_argument("--channels", default="0,1")
    ap.add_argument("--bitrate", type=int, default=500000)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    CONFIG.update(backend=args.backend, channels=args.channels.split(","), bitrate=args.bitrate)

    url = f"http://localhost:{args.port}"
    print(f"\n  BluCANRE Recorder is running.\n\n      Open this in your browser:  {url}\n")
    print("  Press Ctrl+C here when you are finished.\n")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), H)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        if SESSION:
            SESSION.finish()
        print("\n  Stopped. Anything recorded has been saved.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
