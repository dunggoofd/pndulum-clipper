"""Radar: viewer-made Twitch/Kick clips are a virality SIGNAL only. We find the moment and cut it ourselves.

python3 radar.py cut <streamer> <clip_id>     cut one radar moment onto the Clip Desk (To review)
python3 radar.py auto [--top 3] [--min 50]    cut the top N moments per streamer from the last 24h (stream_clips.py runs this)

Never uses the viewer's clip file. Source = our own recording of that minute, else the stream VOD section (Twitch/Kick).

Virality score 0-100 per moment (one moment = viewer clips within 60s of each other, max 90s span):
  core, up to 75 pts = 55% velocity (views/hour percentile within the streamer's own clips) + 45% reach (views/hour, log, 1,000/h = max)
  bonus, up to +25   = clippers: distinct viewers who clipped the same moment (4+ = max)
  bonus, up to +15   = chat: chat messages/sec in the moment vs that hour (4x = max; only when we recorded chat)
  Bonuses only add, so a moment everyone shared as one clip is not penalised. Capped at 100.
"""
import fcntl, json, math, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np, requests

import highlight as h
from views import allowed

CFG, ROOT = h.CFG, h.ROOT
OUT = ROOT / "stream-clips"
STATE = OUT / "radar.json"
BASES = (ROOT / "raw", Path(CFG["live"]), Path(CFG["archive"]))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/130", "Accept": "application/json"}
DONE = {"cutting", "cut", "on desk", "rejected", "no own footage"}


def ts(s):
    d = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def moment(r):
    if r.get("vod_start") is not None and r.get("vod_offset") is not None:
        s = r["vod_start"] + r["vod_offset"]
    else:
        s = ts(r["created_at"]) - r["duration"]
    return s, s + r["duration"]


def peak_of(r):
    return int(moment(r)[1] - 4)


def read_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def mark(cid, **kw):
    with open(OUT / ".radar.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        st = read_state()
        st.setdefault(cid, {}).update(kw, at=time.time())
        STATE.write_text(json.dumps(st, indent=1))


def sessions(name):
    g = {}
    for b in BASES:
        for d in (b / name).glob("[0-9]*"):
            if d.is_dir():
                x = g.setdefault(d.name, {"dirs": [], "segs": []})
                x["dirs"].append(d)
                x["segs"] += h.segments(d)
    for x in g.values():
        x["segs"].sort()
    return g


def chat_times(dirs):
    t = []
    for d in dirs:
        f = d / "chat.jsonl"
        if f.exists():
            for ln in f.read_text(errors="ignore").splitlines():
                try:
                    t.append(json.loads(ln)["t"])
                except (ValueError, KeyError):
                    pass
    return np.array(sorted(t))


def covers(segs, a, b):
    starts = [s for s, _ in segs]
    return all(any(s <= x < s + 62 for s in starts) for x in (a, (a + b) / 2, b))


def own(name, peak, sess=None):
    for x in (sess or sessions(name)).values():
        if x["segs"] and covers(x["segs"], peak - h.LEAD, peak + h.TAIL):
            return x
    return None


def enrich(name, rows):
    """Add moment time, cluster and virality score to this pull's rows (in place)."""
    sess, chats = sessions(name), {}
    for r in rows:
        r["moment_s"], r["moment_e"] = moment(r)
    rows.sort(key=lambda r: r["moment_e"])
    cl, last, first = -1, -1e18, -1e18
    for r in rows:
        if r["moment_e"] - last > 60 or r["moment_e"] - first > 90:
            cl += 1
            first = r["moment_e"]
        last = r["moment_e"]
        r["cluster"] = f"{name}-{cl}"
    groups = {}
    for r in rows:
        groups.setdefault(r["cluster"], []).append(r)
    vph = sorted(r["vph"] for r in rows)
    for r in rows:
        g = groups[r["cluster"]]
        p = {"velocity": float(np.searchsorted(vph, r["vph"], side="right")) / max(len(vph), 1),
             "reach": min(1.0, math.log10(r["vph"] + 1) / 3),
             "clippers": min(1.0, (len({x.get("creator") for x in g}) - 1) / 3)}
        x = own(name, peak_of(r), sess)
        r["own"] = bool(x)
        if x:
            key = x["dirs"][0].name
            if key not in chats:
                chats[key] = chat_times(x["dirs"])
            c = chats[key]
            if len(c) > 50:
                s, e = r["moment_s"] - 5, r["moment_e"] + 10
                n_in = np.searchsorted(c, e) - np.searchsorted(c, s)
                n_hr = np.searchsorted(c, s + 1800) - np.searchsorted(c, s - 1800)
                if n_hr:
                    lift = (n_in / (e - s)) / (n_hr / 3600)
                    r["chat_lift"] = round(float(lift), 2)
                    p["chat"] = max(0.0, min(1.0, (lift - 1) / 3))
        r["parts"] = {k: round(v, 2) for k, v in p.items()}
        r["clippers"] = len({x.get("creator") for x in g})
        r["score"] = min(100, round(75 * (0.55 * p["velocity"] + 0.45 * p["reach"]) + 25 * p["clippers"] + 15 * p.get("chat", 0)))
    for g in groups.values():
        best = max(g, key=lambda r: r["score"])
        for r in g:
            r["best"] = r is best
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def on_desk(name, peak):
    for f in (ROOT / "clips").glob(f"[0-9]*/{name}-*.json"):
        if f.name.endswith(".cover.json"):
            continue
        try:
            r = json.loads(f.read_text())
        except ValueError:
            continue
        if r.get("status") == "cut" and abs(r.get("peak", 0) - peak) <= 45:
            return r["file"]
    return None


def vod_url(name, r, s):
    if r["platform"] == "twitch":
        if not r.get("stream") or r.get("vod_start") is None:
            return None, None
        return f"https://www.twitch.tv/videos/{r['stream']}", r["vod_start"]
    login = s.get("login", name)
    vids = requests.get(f"https://kick.com/api/v2/channels/{login}/videos", headers=UA, timeout=15).json()
    vids = vids if isinstance(vids, list) else vids.get("data", [])
    for v in vids:
        if str(v.get("id")) == str(r.get("stream")) and (v.get("video") or {}).get("uuid") and not v.get("is_live"):
            return f"https://kick.com/video/{v['video']['uuid']}", ts(v["start_time"])
    return None, None


def vod_session(name, r, peak, url, start):
    d = ROOT / "raw" / name / f"radar-{r['id']}"
    d.mkdir(parents=True, exist_ok=True)
    a, b = max(0, peak - h.LEAD - 20 - start), peak + h.TAIL + 20 - start
    src = d / "section.mp4"
    if not src.exists():
        subprocess.run(["yt-dlp", "-q", "--no-warnings", "-f", f"best[height<={CFG['height']}]/best", "--merge-output-format", "mp4",
                        "--download-sections", f"*{a:.0f}-{b:.0f}", "--force-keyframes-at-cuts", "-o", str(src), url], check=True, timeout=900)
    for p in d.glob("*.ts"):
        p.unlink()
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-c", "copy", "-f", "segment", "-segment_time", "60",
                    "-reset_timestamps", "1", str(d / "seg%04d.ts")], check=True)
    at = start + a
    for p in sorted(d.glob("seg*.ts")):
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                                   capture_output=True, text=True).stdout)
        p.rename(d / f"{round(at)}.ts")
        at += dur
    meta = {"id": r.get("stream"), "title": r.get("title", ""), "game": r.get("category", ""), "name": name,
            "platform": r["platform"], "started": start}
    (d / "meta.json").write_text(json.dumps(meta))
    return d, meta, h.segments(d)


def cut(name, cid):
    rows = {r["id"]: r for r in json.loads((OUT / name / "clips.json").read_text())}
    r = rows[cid]
    s = CFG["streamers"][name]
    peak = peak_of(r)
    with open(h.HERE / "logs" / "radar.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        mark(cid, state="cutting", streamer=name, error=None)
        dup = on_desk(name, peak)
        if dup:
            return mark(cid, state="on desk", file=dup)
        x = own(name, peak)
        if x:
            d = next((p for p in x["dirs"] if (p / "meta.json").exists()), x["dirs"][0])
            meta = json.loads((d / "meta.json").read_text())
            segs = x["segs"]
            msgs = []
            for p in x["dirs"]:
                f = p / "chat.jsonl"
                if f.exists():
                    for ln in f.read_text(errors="ignore").splitlines():
                        try:
                            msgs.append(json.loads(ln))
                        except ValueError:
                            pass
            source = "our recording"
        else:
            url, start = vod_url(name, r, s)
            if not url:
                return mark(cid, state="waiting for VOD", error="no stream VOD published yet; retried on the next radar pull")
            d, meta, segs = vod_session(name, r, peak, url, start)
            msgs, source = [], "stream VOD"
        try:
            rec = h.make_clip(d, meta, segs, msgs, peak, r.get("score", 0) / 10)
        except Exception as e:
            return mark(cid, state="error", error=str(e)[:300])
        if rec["status"] != "cut":
            return mark(cid, state="rejected", error=rec.get("judge", {}).get("reason", "judge said weak"))
        cj = (ROOT / "clips" / rec["file"]).with_suffix(".json")
        j = json.loads(cj.read_text())
        j["radar"] = {"id": cid, "page": r["page"], "views": r["views"], "vph": r["vph"], "score": r.get("score"),
                      "clippers": r.get("clippers"), "source": source}
        cj.write_text(json.dumps(j, indent=1))
        mark(cid, state="cut", file=rec["file"], source=source)
        subprocess.run([sys.executable, str(h.HERE / "cover.py"), str(cj)], capture_output=True, text=True, timeout=600)
        h.index()
        h.notify(f"{name} (radar): {rec['judge']['hook']}")


def auto(top=3, min_score=65, hours=24):
    st = read_state()
    cutoff = time.time() - hours * 3600
    for f in sorted(OUT.glob("*/clips.json")):
        name = f.parent.name
        if name not in CFG["streamers"] or not allowed(name):
            continue
        rows = [r for r in json.loads(f.read_text()) if r.get("best") and r.get("score", 0) >= min_score
                and ts(r["created_at"]) > cutoff and st.get(r["id"], {}).get("state") not in DONE]
        for r in sorted(rows, key=lambda r: r["score"], reverse=True)[:top]:
            print(name, r["score"], r["title"][:50], flush=True)
            cut(name, r["id"])
            print("  ->", read_state().get(r["id"], {}).get("state"), flush=True)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["cut"]:
        cut(a[1], a[2])
    elif a[:1] == ["auto"]:
        auto(int(a[a.index("--top") + 1]) if "--top" in a else 3, int(a[a.index("--min") + 1]) if "--min" in a else 65)
    else:
        print(__doc__)
