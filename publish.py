"""Post one approved clip to every allowed platform through Blotato, log the links, then submit each to its campaign.

python3 publish.py <YYYYMMDD/name.mp4> [--platforms tiktok,youtube] [--no-submit] [--force]
python3 publish.py --accounts      list Blotato accounts (setup check)
Key: BLOTATO_API_KEY in .env (repo root)
"""
import argparse, json, subprocess, sys, tempfile, time
from pathlib import Path

import requests

from views import CFG, HERE, ROOT, ledger, read_ledger, safe_tags, safe_text

API = "https://backend.blotato.com/v2"
BLOTATO = {"tiktok": "tiktok", "youtube": "youtube", "instagram": "instagram", "x": "twitter"}


def key():
    return __import__("cfg").env("BLOTATO_API_KEY") or sys.exit("BLOTATO_API_KEY missing from .env")


def call(method, path, **kw):
    for _ in range(3):
        r = requests.request(method, API + path, headers={"blotato-api-key": key()}, timeout=60, **kw)
        if r.status_code != 429:
            break
        time.sleep(60)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} {r.status_code}: {r.text[:300]}")
    return r.json()


def accounts(s=None):
    """One Blotato account per platform: the streamer's own `accounts` if set, else the pinned top-level `accounts`."""
    want = {**(CFG.get("accounts") or {}), **((s or {}).get("accounts") or {})}
    out = {}
    for a in call("GET", "/users/me/accounts")["items"]:
        w = want.get({v: k for k, v in BLOTATO.items()}[a["platform"]]) if a["platform"] in BLOTATO.values() else None
        if w is None or str(w) in (a["id"], a.get("username")):
            out[a["platform"]] = a
    return out


def upload(path, ctype):
    u = call("POST", "/media/uploads", json={"filename": path.name})
    r = requests.put(u["presignedUrl"], data=path.read_bytes(), headers={"Content-Type": ctype}, timeout=600)
    r.raise_for_status()
    return u["publicUrl"]


def caption(rec, s, platform):
    j = rec["judge"]
    text = safe_text(j.get("caption", "").strip()) or ""
    if text and text[-1] not in ".!?" and s.get("tags") and not text.split()[-1].startswith(("#", "@")):
        text += "."
    have = text.lower()
    extra = [t for t in s.get("tags", []) if t.lower() not in have]
    have += " " + " ".join(extra).lower()
    tags = [f"#{h}" for h in safe_tags(j.get("hashtags", [])) if f"#{h}".lower() not in have.split()]
    if s.get("disclose"):
        extra.append("#ad")
    out = " ".join([text] + extra + tags)
    cap = min(5 if platform == "instagram" else 99, s.get("max_tags", 99))
    if cap < 99:
        n, keep = 0, []
        for w in out.split(" "):
            if w.startswith("#"):
                n += 1
                if n > cap:
                    continue
            keep.append(w)
        out = " ".join(keep)
    if platform == "youtube":
        out += " #shorts"
    if platform == "x" and len(out) > 280:
        out = " ".join([text] + extra)[:280]
    return out


def with_cover_frame(clip, cover):
    """Clip with the designed cover as a one-frame intro, so TikTok's grid thumbnail (a video frame) shows the safe-zone cover."""
    out = Path(tempfile.mkdtemp()) / clip.name
    fc = ("[0:v]scale=1080:1920,setsar=1,fps=30,format=yuv420p[c];[1:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30,format=yuv420p[v];"
          "[2:a]aresample=48000,aformat=channel_layouts=stereo[ca];[1:a]aresample=48000,aformat=channel_layouts=stereo[va];"
          "[c][ca][v][va]concat=n=2:v=1:a=1[ov][oa]")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", "30", "-t", "0.0334", "-i", str(cover), "-i", str(clip),
                    "-f", "lavfi", "-t", "0.0334", "-i", "anullsrc=r=48000:cl=stereo", "-filter_complex", fc, "-map", "[ov]", "-map", "[oa]",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(out)], check=True)
    return out


def target(platform, rec, s, cover_url):
    if platform == "tiktok":
        return {"targetType": "tiktok", "privacyLevel": "PUBLIC_TO_EVERYONE", "disabledComments": False,
                "disabledDuet": False, "disabledStitch": False, "isBrandedContent": bool(s.get("disclose")),
                "isYourBrand": False, "isAiGenerated": False, "videoCoverTimestamp": 0}
    if platform == "youtube":
        return {"targetType": "youtube", "title": (safe_text(rec["judge"]["hook"]) or rec["streamer"]).replace("<", "").replace(">", "")[:100],
                "privacyStatus": "public", "shouldNotifySubscribers": True, "isMadeForKids": False,
                "containsSyntheticMedia": False}
    if platform == "instagram":
        t = {"targetType": "instagram", "mediaType": "reel", "shareToFeed": True}
        if cover_url:
            t["coverImageUrl"] = cover_url
        return t
    return {"targetType": "twitter"}


def mark(f, p, **kw):
    with ledger() as d:
        d["clips"].setdefault(f, {}).setdefault("publish", {}).setdefault(p, {}).update(kw, at=time.time())


def wait_published(sub_id):
    end = time.time() + 900
    while time.time() < end:
        time.sleep(12)
        s = call("GET", f"/posts/{sub_id}")
        if s["status"] == "published":
            return s.get("publicUrl")
        if s["status"] == "failed":
            raise RuntimeError(s.get("errorMessage") or "Blotato says failed")
    raise RuntimeError("still not published after 15 min, check my.blotato.com")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?")
    ap.add_argument("--platforms")
    ap.add_argument("--no-submit", action="store_true")
    ap.add_argument("--accounts", action="store_true")
    ap.add_argument("--force", action="store_true", help="skip the governor gate (Dung's explicit call only)")
    ap.add_argument("--unsafe", action="store_true", help="skip the campaign compliance gate (never from automation)")
    a = ap.parse_args()
    if a.accounts:
        for p, acc in accounts().items():
            print(p, acc["id"], acc.get("username"))
        return
    clip = ROOT / "clips" / a.file
    rec = json.loads(clip.with_suffix(".json").read_text())
    s = CFG["streamers"][rec["streamer"]]
    allowed = s.get("post_to", CFG["platforms"])
    plats = [p for p in (a.platforms.split(",") if a.platforms else allowed) if p in allowed]
    accs = accounts(s)
    missing = [p for p in plats if BLOTATO[p] not in accs]
    for p in missing:
        mark(a.file, p, state="failed", error=f"{p} not connected in Blotato")
    plats = [p for p in plats if p not in missing]
    if s.get("off_niche") and not s.get("accounts"):
        sys.exit(f"{rec['streamer']} is off-niche for this account set")
    import governor, gate
    for p in list(plats):
        why = None if a.force else governor.gate(p)
        if why:
            mark(a.file, p, state="failed", error=f"governor: {why}")
            plats.remove(p)
            continue
        bad = [] if a.unsafe else gate.compliance(rec, s, p, caption(rec, s, p))
        if bad:
            mark(a.file, p, state="failed", error="rules: " + "; ".join(bad))
            print("blocked", p, "; ".join(bad), flush=True)
            plats.remove(p)
    if not plats:
        return
    for p in plats:
        mark(a.file, p, state="uploading", error=None)
    cover = clip.with_suffix(".cover.jpg")
    src = with_cover_frame(clip, cover) if cover.exists() else clip
    video = upload(src, "video/mp4")
    if src != clip:
        import shutil
        shutil.rmtree(src.parent, ignore_errors=True)
    cover_url = upload(cover, "image/jpeg") if cover.exists() and "instagram" in plats else None
    subs = {}
    for p in plats:
        body = {"post": {"accountId": accs[BLOTATO[p]]["id"],
                         "content": {"text": caption(rec, s, p), "mediaUrls": [video], "platform": BLOTATO[p]},
                         "target": target(p, rec, s, cover_url)}}
        try:
            subs[p] = call("POST", "/posts", json=body)["postSubmissionId"]
            mark(a.file, p, state="publishing", blotato_id=subs[p])
        except Exception as e:
            mark(a.file, p, state="failed", error=str(e))
    for p, sid in subs.items():
        try:
            url = wait_published(sid)
        except Exception as e:
            mark(a.file, p, state="failed", error=str(e))
            continue
        if not url:
            mark(a.file, p, state="failed", error="published, but Blotato returned no link: paste it on the card")
            continue
        with ledger() as d:
            c = d["clips"].setdefault(a.file, {})
            c.setdefault("posts", {})[p] = {"url": url, "posted": time.time(), "views": None, "submitted": False,
                                            "via": "blotato"}
            c.setdefault("stage", "approved")
        mark(a.file, p, state="published")
        print("published", p, url, flush=True)
        if s.get("campaign_url") and not a.no_submit:
            import submit
            submit.run(a.file, p)


if __name__ == "__main__":
    main()
