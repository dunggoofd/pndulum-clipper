"""Clip Desk: every clipper moment and its money pipeline. http://127.0.0.1:7779 (systemd gtaclip-dash.service)"""
import json, re, shutil, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from views import CFG, HERE, ROOT, ledger, read_ledger, no_logos
import governor
import publish

WEB = HERE / "web" / "index.html"
CLIPS = ROOT / "clips"
RAWS = (ROOT / "raw", Path(CFG["live"]))
FILE_RE = re.compile(r"^\d{8}/[\w-]+\.mp4$")
PLATFORMS = CFG["platforms"]
refresh_proc = None
cover_procs = {}
pub_procs = {}


def running(k):
    return bool(pub_procs.get(k) and pub_procs[k].poll() is None)


def posting(f):
    """Platforms with a publish.py running for this clip: the whole clip (Post to all) or one platform."""
    return PLATFORMS[:] if running(f) else [p for p in PLATFORMS if running((f, p))]
radar_procs = {}
radar_pull = None
scout_proc = None
SCOUT = ROOT / "scout" / "latest.json"
RADAR = ROOT / "stream-clips"
RID_RE = re.compile(r"^[\w-]+$")
T_RE = re.compile(r"^\d+(\.\d+)?$")


def rate_for(name, rates):
    if name in rates:
        return rates[name]
    return CFG["streamers"].get(name, {}).get("rate_per_1k")


def recorders():
    out = []
    for name in (n for n, s in CFG["streamers"].items() if not s.get("vod_only")):
        r = subprocess.run(["systemctl", "--user", "show", f"gtaclip-{name}.service", "-p", "MainPID", "--value"], capture_output=True, text=True)
        pid = r.stdout.strip() not in ("", "0")   # MainPID is 0 when the unit isn't running
        newest = [max((r / name).glob("*/"), default=None) for r in RAWS]
        segs = sorted((p for d in newest if d for p in d.glob("[0-9]*.ts")), key=lambda p: p.stat().st_mtime)
        last = segs[-1].stat().st_mtime if segs else None
        logf = HERE / "logs" / f"{name}.log"
        line = logf.read_text(errors="ignore").strip().splitlines()[-1][:160] if logf.exists() and logf.stat().st_size else ""
        fresh = bool(last and time.time() - last < 300)
        live = "recording" in line or "restarting the pipe" in line
        out.append(dict(name=name, running=bool(pid), last_seg=last, log=line,
                        state="recording" if pid and fresh else "stalled" if pid and live else "offline" if pid else "stopped",
                        platform=CFG["streamers"][name]["platform"]))
    hl = HERE / "logs" / "highlight.log"
    hline = hl.read_text(errors="ignore").strip().splitlines()[-1][:160] if hl.exists() and hl.stat().st_size else ""
    return out, dict(last=hl.stat().st_mtime if hl.exists() else None, log=hline)


DRIVE = dict(recs=[dict(name=n, running=False, last_seg=None, log="", state="checking drive", platform=s["platform"])
                   for n, s in CFG["streamers"].items() if not s.get("vod_only")],
             hl=dict(last=None, log=""), moments={}, at=None)


def drive_loop():
    while True:
        try:
            recs, hl = recorders()
            DRIVE.update(recs=recs, hl=hl, moments=moments(), at=time.time())
        except OSError:
            pass
        time.sleep(20)


def moments():
    counts = {}
    for f in (f for r in RAWS for f in r.glob("*/[0-9]*/moments.json")):
        for m in json.loads(f.read_text()):
            counts[m["status"]] = counts.get(m["status"], 0) + 1
    return counts


def pace(clips):
    cap, now, out = CFG.get("daily_cap") or {}, time.time(), {}
    for pl in PLATFORMS:
        ps = sorted((x for c in clips for q, x in c["posts"].items() if q == pl), key=lambda x: x.get("posted") or 0)
        aged = [x["views"] for x in ps if x.get("views") is not None and now - (x.get("posted") or now) >= 48 * 3600][-10:]
        med = sorted(aged)[len(aged) // 2] if aged else None
        n = cap.get(pl)
        verdict = ("no cap set" if n is None else f"need {5 - len(aged)} more posts aged 48h+" if len(aged) < 5
                   else f"step up to {n + 1}/day" if med >= 500 else "hold, median under 500")
        out[pl] = dict(cap=n, day=sum(1 for x in ps if now - (x.get("posted") or 0) < 86400), median=med, n=len(aged),
                       ready=bool(n is not None and len(aged) >= 5 and med >= 500), verdict=verdict,
                       gov=governor.gate(pl))
    return out


def state():
    led = read_ledger()
    rates = led.get("rates", {})
    clips = []
    for f in sorted(CLIPS.glob("[0-9]*/[!.]*.json"), reverse=True):
        r = json.loads(f.read_text())
        if r.get("status") != "cut":
            continue
        c = led["clips"].get(r["file"], {})
        rate = rate_for(r["streamer"], rates)
        posts = c.get("posts", {})
        for p in posts.values():
            p["est"] = round(p["views"] / 1000 * rate, 2) if rate is not None and p.get("views") else None
        stage = ("paid" if c.get("paid") is not None else "rejected" if c.get("stage") == "rejected" else "posted" if posts else c.get("stage") or "review")
        j = r["judge"]
        clips.append(dict(file=r["file"], streamer=r["streamer"], campaign=r.get("campaign", ""), peak=r.get("peak") or f.stat().st_mtime,
                          duration=r.get("duration"), score=j.get("score"), signal=r.get("signal"), hook=j.get("hook", ""),
                          caption=j.get("caption", ""), reason=j.get("reason", ""), layout=r.get("layout"),
                          hooks=j.get("hooks") or [j.get("hook", "")], snub_end=r.get("snub_end") if j.get("snub") else None,
                          hashtags=j.get("hashtags") or ["gta6", r["streamer"]],
                          cover=r.get("cover"), cover_busy=bool(cover_procs.get(r["file"]) and cover_procs[r["file"]].poll() is None),
                          stage=stage, posts=posts, paid=c.get("paid"), rate=rate,
                          publish=c.get("publish", {}), publishing=bool(posting(r["file"])), posting=posting(r["file"]),
                          post_to=CFG["streamers"].get(r["streamer"], {}).get("post_to", PLATFORMS),
                          onscreen=CFG["streamers"].get(r["streamer"], {}).get("onscreen"),
                          captions={p: publish.caption(r, CFG["streamers"].get(r["streamer"], {}), p) for p in PLATFORMS},
                          window=CFG["streamers"].get(r["streamer"], {}).get("submit_window_min"),
                          views=sum(p.get("views") or 0 for p in posts.values()),
                          est=round(sum(p["est"] or 0 for p in posts.values()), 2)))
    recs, hl = DRIVE["recs"], DRIVE["hl"]
    camps = []
    for name, s in CFG["streamers"].items():
        mine = [c for c in clips if c["streamer"] == name]
        camps.append(dict(streamer=name, campaign=s.get("campaign", "none"), paid_campaign=bool(s.get("campaign")),
                          rate=rate_for(name, rates), window=s.get("submit_window_min"), cut=len(mine),
                          posted=sum(1 for c in mine if c["posts"]), views=sum(c["views"] for c in mine),
                          est=round(sum(c["est"] for c in mine), 2), paid=round(sum(c["paid"] or 0 for c in mine), 2)))
    return dict(now=time.time(), clips=clips, pace=pace(clips), moments=DRIVE["moments"], drive_at=DRIVE["at"], recorders=recs, highlight=hl, campaigns=camps,
                platforms=PLATFORMS, refreshing=bool(refresh_proc and refresh_proc.poll() is None), radar=radar_rows(),
                radar_pulling=bool(radar_pull and radar_pull.poll() is None),
                scout=json.loads(SCOUT.read_text()) if SCOUT.exists() else {"rows": []},
                scouting=bool(scout_proc and scout_proc.poll() is None))


def radar_rows(hours=48):
    st = json.loads((RADAR / "radar.json").read_text()) if (RADAR / "radar.json").exists() else {}
    cutoff, out = time.time() - hours * 3600, []
    for f in RADAR.glob("*/clips.json"):
        name = f.parent.name
        if name not in CFG["streamers"]:
            continue
        rows = json.loads(f.read_text())
        n = {}
        for r in rows:
            n[r.get("cluster")] = n.get(r.get("cluster"), 0) + 1
        for r in rows:
            if not r.get("best") or "score" not in r or r.get("moment_e", 0) < cutoff:
                continue
            fl = r.get("file")
            x = st.get(r["id"], {})
            if radar_procs.get(r["id"]) and radar_procs[r["id"]].poll() is None and x.get("state") not in ("cutting",):
                x = {**x, "state": "queued"}
            out.append(dict(id=r["id"], streamer=name, title=r["title"], views=r["views"], vph=r["vph"], score=r["score"],
                            parts=r.get("parts", {}), clippers=r.get("clippers", 1), chat_lift=r.get("chat_lift"), own=r.get("own"),
                            reach=no_logos(CFG["streamers"][name]), at=r.get("moment_e"), duration=r["duration"],
                            category=r.get("category", ""), platform=CFG["streamers"][name].get("platform"), page=r["page"], thumb=r.get("thumb"), n=n.get(r.get("cluster"), 1),
                            preview=f"{name}/{Path(fl).name}" if fl and Path(fl).exists() else None,
                            state=x.get("state"), error=x.get("error"), file=x.get("file"), source=x.get("source")))
    return sorted(out, key=lambda r: r["score"], reverse=True)


def trash(f):
    src = CLIPS / f
    bin_ = Path.home() / ".Trash" if (Path.home() / ".Trash").is_dir() else ROOT / "trash"
    bin_.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    for p in [*src.parent.glob(f"{src.stem}.*"), src.parent / "covers" / src.stem]:
        if p.exists():
            shutil.move(str(p), str(bin_ / f"{p.name}.{stamp}" if (bin_ / p.name).exists() else bin_ / p.name))


def apply(body):
    f = body.get("file", "")
    if not FILE_RE.match(f) or not (CLIPS / f).exists():
        raise ValueError("unknown clip")
    op, p = body.get("op"), body.get("platform")
    if p is not None and p not in PLATFORMS:
        raise ValueError("unknown platform")
    with ledger() as d:
        c = d["clips"].setdefault(f, {})
        posts = c.setdefault("posts", {})
        if op in ("approve", "reject"):
            c["stage"] = {"approve": "approved", "reject": "rejected"}[op]
        elif op == "reset":
            c.pop("stage", None)
        elif op == "delete":
            if posts or posting(f):
                raise ValueError("posted clips can't be deleted, remove the post links first")
            trash(f)
            d["clips"].pop(f, None)
        elif op == "post":
            url = body.get("url", "").strip()
            if not url.startswith("http"):
                raise ValueError("paste the full post URL")
            posts[p] = {"url": url, "posted": time.time(), "views": None, "submitted": False}
            c.setdefault("stage", "approved")
        elif op == "unpost":
            posts.pop(p, None)
        elif op == "views":
            posts[p].update(views=int(body["views"]), source="manual", checked=time.time(), error=None)
        elif op == "submitted":
            posts[p].update(submitted=bool(body.get("value")), submitted_at=time.time() if body.get("value") else None)
        elif op == "paid":
            c["paid"] = None if body.get("amount") in (None, "") else round(float(body["amount"]), 2)
        else:
            raise ValueError("unknown op")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, data, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def js(self, obj, code=200):
        self.send(code, json.dumps(obj).encode(), "application/json")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self.send(200, WEB.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self.js(state())
        if path.startswith("/poster/"):
            f = path[8:]
            if not FILE_RE.match(f) or not (CLIPS / f).exists():
                return self.send(404, b"", "text/plain")
            jpg = (CLIPS / f).with_suffix(".jpg")
            if not jpg.exists():
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1.5", "-i", str(CLIPS / f), "-frames:v", "1",
                                "-vf", "scale=360:-2", "-q:v", "4", str(jpg)])
            return self.send(200, jpg.read_bytes(), "image/jpeg") if jpg.exists() else self.send(404, b"", "text/plain")
        if path.startswith("/cover/"):
            f = path[7:]
            jpg = (CLIPS / f).with_suffix(".cover.jpg")
            if not FILE_RE.match(f) or not jpg.exists():
                return self.send(404, b"", "text/plain")
            dl = {"Content-Disposition": f'attachment; filename="{jpg.name}"'} if "dl=1" in self.path else {}
            return self.send(200, jpg.read_bytes(), "image/jpeg", dl)
        if path.startswith("/cand/"):
            f, _, t = path[6:].rpartition("/")
            t = t.removesuffix(".jpg")
            jpg = CLIPS / Path(f).parent / "covers" / Path(f).stem / f"{t}.jpg"
            if not FILE_RE.match(f) or not T_RE.match(t) or not jpg.exists():
                return self.send(404, b"", "text/plain")
            return self.send(200, jpg.read_bytes(), "image/jpeg")
        if path.startswith("/media/") or path.startswith("/radar-media/"):
            radar = path.startswith("/radar-media/")
            f = path[13:] if radar else path[7:]
            base = RADAR if radar else CLIPS
            ok = re.match(r"^[\w-]+/[\w.-]+\.mp4$", f) if radar else FILE_RE.match(f)
            if not ok or ".." in f or not (base / f).exists():
                return self.send(404, b"", "text/plain")
            data = (base / f).read_bytes()
            n = len(data)
            dl = {"Content-Disposition": f'attachment; filename="{Path(f).name}"'} if "dl=1" in self.path else {}
            m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
            if not m:
                return self.send(200, data, "video/mp4", {"Accept-Ranges": "bytes", **dl})
            start = int(m[1]) if m[1] else n - int(m[2])
            end = int(m[2]) if m[1] and m[2] else n - 1
            if start >= n:
                return self.send(416, b"", "video/mp4", {"Content-Range": f"bytes */{n}"})
            end = min(end, n - 1)
            return self.send(206, data[start:end + 1], "video/mp4",
                             {"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{n}"})
        self.send(404, b"", "text/plain")

    def do_POST(self):
        global refresh_proc, radar_pull, scout_proc
        path = urlparse(self.path).path
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        try:
            if path == "/api/clip":
                apply(body)
            elif path == "/api/rate":
                if body.get("streamer") not in CFG["streamers"]:
                    raise ValueError("unknown streamer")
                with ledger() as d:
                    rates = d.setdefault("rates", {})
                    if body.get("rate") in (None, ""):
                        rates.pop(body["streamer"], None)
                    else:
                        rates[body["streamer"]] = float(body["rate"])
            elif path == "/api/cover":
                f = body.get("file", "")
                if not FILE_RE.match(f) or not (CLIPS / f).exists():
                    raise ValueError("unknown clip")
                if cover_procs.get(f) and cover_procs[f].poll() is None:
                    raise ValueError("cover already rendering")
                cmd = [sys.executable, str(HERE / "cover.py"), str((CLIPS / f).with_suffix(".json"))]
                if body.get("t") is not None:
                    cmd += ["--t", str(float(body["t"]))]
                if (body.get("text") or "").strip():
                    cmd += ["--text", body["text"].strip()[:80]]
                log = open(HERE / "logs" / "cover.log", "a")
                cover_procs[f] = subprocess.Popen(cmd, stdout=log, stderr=log, cwd=HERE)
            elif path in ("/api/publish", "/api/submit"):
                f = body.get("file", "")
                if not FILE_RE.match(f) or not (CLIPS / f).exists():
                    raise ValueError("unknown clip")
                pl = body.get("platform")
                if pl is not None and pl not in PLATFORMS:
                    raise ValueError("unknown platform")
                key = (f, pl) if pl else f
                if running(f) or (pl and running(key)) or (not pl and posting(f)):
                    raise ValueError("already posting this clip" + (f" to {pl}" if pl else ""))
                if path == "/api/publish":
                    c = read_ledger()["clips"].get(f, {})
                    if c.get("stage") != "approved":
                        raise ValueError("approve the clip first")
                    if pl and pl in c.get("posts", {}):
                        raise ValueError(f"already posted to {pl}")
                    cmd = [sys.executable, str(HERE / "publish.py"), f] + (["--platforms", pl] if pl else []) + (["--force"] if body.get("force") is True else [])
                else:
                    if not pl:
                        raise ValueError("unknown platform")
                    cmd = [sys.executable, str(HERE / "submit.py"), f, pl]
                log = open(HERE / "logs" / "publish.log", "a")
                pub_procs[key] = subprocess.Popen(cmd, stdout=log, stderr=log, cwd=HERE)
            elif path == "/api/radar/cut":
                name, rid = body.get("streamer"), body.get("id", "")
                if name not in CFG["streamers"] or not RID_RE.match(rid):
                    raise ValueError("unknown radar clip")
                if radar_procs.get(rid) and radar_procs[rid].poll() is None:
                    raise ValueError("already cutting this moment")
                log = open(HERE / "logs" / "radar.log", "a")
                radar_procs[rid] = subprocess.Popen([sys.executable, str(HERE / "radar.py"), "cut", name, rid], stdout=log, stderr=log, cwd=HERE)
            elif path == "/api/radar/pull":
                if not (radar_pull and radar_pull.poll() is None):
                    log = open(HERE / "logs" / "streamclips.log", "a")
                    radar_pull = subprocess.Popen([sys.executable, str(HERE / "stream_clips.py"), "--no-auto"], stdout=log, stderr=log, cwd=HERE)
            elif path == "/api/scout":
                if not (scout_proc and scout_proc.poll() is None):
                    log = open(HERE / "logs" / "scout.log", "a")
                    scout_proc = subprocess.Popen([sys.executable, str(HERE / "scout.py")], stdout=log, stderr=log, cwd=HERE)
            elif path == "/api/refresh":
                if not (refresh_proc and refresh_proc.poll() is None):
                    log = open(HERE / "logs" / "views.log", "a")
                    refresh_proc = subprocess.Popen([sys.executable, str(HERE / "views.py")], stdout=log, stderr=log, cwd=HERE)
            else:
                return self.send(404, b"", "text/plain")
        except (ValueError, KeyError, TypeError) as e:
            return self.js({"error": str(e)}, 400)
        self.js(state())


if __name__ == "__main__":
    threading.Thread(target=drive_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", CFG["dash_port"]), H).serve_forever()
