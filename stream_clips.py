"""Pull viewer-made clips from Kick and Twitch for each streamer in config.yaml, rank by views/hour,
download the top ones at 720p, rebuild the review page.

python3 stream_clips.py                  all kick + twitch streamers, last day, top 5 each
python3 stream_clips.py lacy --top 10 --hours 72
python3 stream_clips.py --index          rebuild the review page only
"""
import argparse, html, json, os, subprocess, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests, yaml

import radar

HERE = Path(__file__).parent
CFG = __import__("cfg").load()
OUT = Path(CFG["root"]) / "stream-clips"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/130", "Accept": "application/json"}
from cfg import env


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def kick(name, hours):
    window = "day" if hours <= 24 else "week" if hours <= 168 else "month"
    clips, cursor = [], None
    for _ in range(3):
        params = {"sort": "view", "time": window, **({"cursor": cursor} if cursor else {})}
        d = requests.get(f"https://kick.com/api/v2/channels/{name}/clips", params=params, headers=UA, timeout=15).json()
        clips += d.get("clips", [])
        cursor = d.get("nextCursor")
        if not cursor:
            break
    return [{"id": c["id"], "title": c["title"], "views": c.get("view_count") or c.get("views") or 0,
             "duration": c["duration"], "created_at": c["created_at"], "stream": c["livestream_id"],
             "category": (c.get("category") or {}).get("name", ""), "creator": (c.get("creator") or {}).get("username", ""),
             "src": c.get("video_url") or c["clip_url"], "thumb": c.get("thumbnail_url"),
             "page": f"https://kick.com/{name}/clips/{c['id']}"} for c in clips]


_tw = {}


def twitch_headers():
    if not _tw:
        cid, sec = env("TWITCH_CLIENT_ID"), env("TWITCH_CLIENT_SECRET")
        tok = requests.post("https://id.twitch.tv/oauth2/token", timeout=15,
                            data={"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"}).json()["access_token"]
        _tw.update({"Client-Id": cid, "Authorization": f"Bearer {tok}"})
    return _tw


def twitch(name, hours):
    h = twitch_headers()
    uid = requests.get("https://api.twitch.tv/helix/users", params={"login": name}, headers=h, timeout=15).json()["data"][0]["id"]
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    d = requests.get("https://api.twitch.tv/helix/clips", params={"broadcaster_id": uid, "started_at": since, "first": 100},
                     headers=h, timeout=15).json()["data"]
    games = {}
    ids = list({c["game_id"] for c in d if c["game_id"]})[:100]
    if ids:
        games = {g["id"]: g["name"] for g in requests.get("https://api.twitch.tv/helix/games", params={"id": ids}, headers=h, timeout=15).json()["data"]}
    vids = list({c["video_id"] for c in d if c["video_id"]})[:100]
    starts = {v["id"]: ts(v["created_at"]) for v in requests.get("https://api.twitch.tv/helix/videos", params={"id": vids}, headers=h,
                                                                  timeout=15).json().get("data", [])} if vids else {}
    return [{"id": c["id"], "title": c["title"], "views": c["view_count"], "duration": round(c["duration"]),
             "created_at": c["created_at"], "stream": c["video_id"], "category": games.get(c["game_id"], ""),
             "vod_offset": c.get("vod_offset"), "vod_start": starts.get(c["video_id"]),
             "creator": c["creator_name"], "src": c["url"], "thumb": c["thumbnail_url"], "page": c["url"]} for c in d]


def download(r, d):
    out = d / f"{r['created_at'][:10]}_{r['id']}.mp4"
    if not out.exists():
        subprocess.run(["yt-dlp", "-q", "--no-warnings", "-f", "best[height<=720]/best", "--remux-video", "mp4",
                        "-o", str(out), r["src"]], check=True)
    return out


def run(name, platform, top, hours):
    d = OUT / name
    d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    login = CFG["streamers"].get(name, {}).get("login", name)
    rows = kick(login, hours) if platform == "kick" else twitch(login, hours)
    for r in rows:
        r.update(streamer=name, platform=platform, vph=round(r["views"] / max((now - ts(r["created_at"])) / 3600, 0.25), 1))
    radar.enrich(name, rows)
    idx = d / "clips.json"
    old = {r["id"]: r for r in json.loads(idx.read_text())} if idx.exists() else {}
    for r in rows[:top]:
        try:
            r["file"] = str(download(r, d))
        except subprocess.CalledProcessError as e:
            print("  download failed", r["id"], e)
    old.update({r["id"]: {**old.get(r["id"], {}), **r} for r in rows})
    idx.write_text(json.dumps(sorted(old.values(), key=lambda r: r.get("score", 0), reverse=True), indent=1))
    print(f"{name} ({platform}): {len(rows)} clips, top {min(top, len(rows))}")
    for r in rows[:top]:
        print(f"  score {r['score']:>3}  {r['vph']:>7}/h {r['views']:>6}v {r['clippers']} clippers  {r['title'][:50]}  [{r['category']}]")


def index(hours=48):
    cutoff = time.time() - hours * 3600
    rows = [r for f in OUT.glob("*/clips.json") for r in json.loads(f.read_text()) if r.get("file") and Path(r["file"]).exists()
            and ts(r["created_at"]) > cutoff]
    rows.sort(key=lambda r: r["vph"], reverse=True)
    cards = "".join(f"""<article><video src="{html.escape(r['file'])}" controls preload="metadata" playsinline></video>
<div class="m"><div class="k">{html.escape(r['streamer'])} · {r['platform']} · {html.escape(r['category'])} · {datetime.fromtimestamp(ts(r['created_at'])).strftime('%d %b %H:%M')} · {r['duration']}s</div>
<h2>{r['vph']}/h <span>{r['views']} views</span></h2><p class="c">{html.escape(r['title'])}</p>
<p class="r">clipped by {html.escape(r['creator'])} · <a href="{html.escape(r['page'])}">source</a></p></div></article>""" for r in rows)
    page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Stream Clip Radar</title>
<style>:root{{--bg:#0b0b0c;--card:#151517;--ink:#f2f2f2;--mute:#9a9aa0;--acc:#53fc18}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,Inter,sans-serif;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}}.sub{{color:var(--mute);margin-bottom:20px}}a{{color:var(--mute)}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}}
article{{background:var(--card);border-radius:12px;overflow:hidden}}video{{width:100%;aspect-ratio:16/9;background:#000;display:block}}
.m{{padding:12px}}.k{{color:var(--mute);font-size:12px}}h2{{font-size:18px;margin:6px 0;color:var(--acc)}}h2 span{{font-size:13px;color:var(--mute);font-weight:400}}
.r{{color:var(--mute);font-size:13px}}</style>
<h1>Stream Clip Radar</h1><div class="sub">{len(rows)} viewer clips from Kick + Twitch, last {hours}h · ranked by views/hour · rebuilt {datetime.now().strftime('%d %b %H:%M')} · your chat-spike cuts: <a href="../clips/index.html">GTA Clip Queue</a></div><main>{cards}</main>"""
    (OUT / "index.html").write_text(page)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--no-auto", action="store_true", help="skip auto-cutting the top radar moments")
    a = ap.parse_args()
    if not a.index:
        names = a.names or [n for n, s in CFG["streamers"].items() if s["platform"] in ("kick", "twitch")]
        for n in names:
            p = CFG["streamers"][n]["platform"]
            if p == "twitch" and not env("TWITCH_CLIENT_ID"):
                print(f"{n}: skipped, no TWITCH_CLIENT_ID in {ENV}")
                continue
            try:
                run(n, p, a.top, a.hours)
            except Exception as e:
                print(f"{n}: failed {e}")
    index()
    if not a.index and not a.no_auto:
        radar.auto()


if __name__ == "__main__":
    main()
