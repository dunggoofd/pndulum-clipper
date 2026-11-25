"""Cut clips from one section of a past stream (campaigns that only accept a single VOD) onto the Clip Desk.

python3 vod.py <streamer> <url> <start> <end>    download + split only, e.g. vod.py ishowspeed https://youtu.be/Kyv61l2vF6M 2:46:00 2:52:40
python3 vod.py <session dir> [2:46:57 2:50:14 ...] judge pass: one 52s window per VOD start time given, else every 45s
Session dir: ~/gta-clipper/raw/<streamer>/vod-<id>/ (kept out of the live highlight + archive globs).
Each window goes to the judge; keepers are cut, covered and land in clips/ like live moments.
"""
import json, os, re, subprocess, sys
from pathlib import Path

import highlight as h


def secs(x):
    if x == "inf":
        return None
    n = 0
    for p in x.split(":"):
        n = n * 60 + float(p)
    return n


def download(name, url, start, end):
    local = Path(url).expanduser()
    if local.is_file():
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(local)],
                                   capture_output=True, text=True).stdout)
        info = {"id": re.sub(r"[^a-z0-9]+", "-", local.parent.name.lower()).strip("-"), "title": local.stem, "duration": dur,
                "timestamp": int(local.stat().st_mtime)}
    else:
        info = json.loads(subprocess.run(["yt-dlp", "-J", "--skip-download", url], capture_output=True, text=True, check=True).stdout)
    d = local.parent if local.is_file() else h.ROOT / "raw" / name / f"vod-{info['id']}"
    d.mkdir(parents=True, exist_ok=True)
    s0, s1 = secs(start), secs(end) or info["duration"]
    src = d / "section.mp4"
    if local.is_file() and local != src and not src.exists():
        os.link(local, src)
    if not src.exists():
        subprocess.run(["yt-dlp", "-q", "--no-warnings", "-f", f"bv*[height<={h.CFG['height']}][ext=mp4]+ba[ext=m4a]/b", "--merge-output-format", "mp4",
                        "--download-sections", f"*{s0}-{s1}", "--force-keyframes-at-cuts", "-o", str(src), url], check=True)
    t0 = int((info.get("release_timestamp") or info["timestamp"]) + s0)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-c", "copy", "-f", "segment", "-segment_time", "60",
                    "-reset_timestamps", "1", str(d / "seg%04d.ts")], check=True)
    at = t0
    for p in sorted(d.glob("seg*.ts")):
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                                   capture_output=True, text=True).stdout)
        p.rename(d / f"{round(at)}.ts")
        at += dur
    (d / "chat.jsonl").touch()
    (d / "meta.json").write_text(json.dumps({"id": info["id"], "title": info["title"], "game": "vod", "name": name,
                                             "platform": "youtube", "started": t0, "vod_offset": s0}))
    return d


def judge_pass(d, starts=()):
    meta = json.loads((d / "meta.json").read_text())
    segs = h.segments(d)
    state_f = d / "moments.json"
    state = json.loads(state_f.read_text()) if state_f.exists() else []
    done = {m["peak"] for m in state}
    end = segs[-1][0] + 60
    base = segs[0][0] - meta.get("vod_offset", 0)
    peaks = [int(base + secs(s)) + h.LEAD for s in starts] or range(segs[0][0] + h.LEAD, end - h.TAIL, 45)
    sc = h.CFG["streamers"][meta["name"]]
    avoid, check = sc.get("vod_avoid", []), sc.get("vod_check", [])
    for peak in peaks:
        a0, a1 = peak - base - h.LEAD, peak - base + h.TAIL
        if peak in done or any(a0 < e and a1 > s for s, e in avoid):
            continue
        try:
            rec = h.make_clip(d, meta, segs, [], peak, 0.0)
        except Exception as e:
            rec = {"peak": peak, "signal": 0.0, "status": "error", "error": str(e)[:300]}
            if "judge failed" in str(e):
                print("judge failed, stopping:", e, flush=True)
                break
        state.append(rec)
        state_f.write_text(json.dumps(state, indent=1))
        print(peak - segs[0][0], rec["status"], rec.get("judge", {}).get("hook", ""), rec.get("judge", {}).get("reason", ""), flush=True)
        if rec["status"] == "cut" and check:
            ci, co = a0 + float(rec["judge"]["in"]), a0 + float(rec["judge"]["out"])
            notes = [n for t, n in check if ci - 2 <= t <= co + 2]
            if notes:
                cj = (h.ROOT / "clips" / rec["file"]).with_suffix(".json")
                j = json.loads(cj.read_text())
                j["judge"]["reason"] = "CHECK BEFORE POSTING: " + "; ".join(notes) + ". " + j["judge"].get("reason", "")
                j["checks"] = notes
                cj.write_text(json.dumps(j, indent=1))
        if rec["status"] == "cut":
            subprocess.run([sys.executable, str(h.HERE / "cover.py"), str((h.ROOT / "clips" / rec["file"]).with_suffix(".json"))],
                           capture_output=True, text=True, timeout=600)
    h.index()


if __name__ == "__main__":
    a = sys.argv[1:]
    if Path(a[0]).is_dir():
        judge_pass(Path(a[0]), a[1:])
    else:
        print(download(*a))
