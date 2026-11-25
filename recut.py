"""Re-render cut clips with the streamer's CURRENT banner setting (e.g. after a no-logos pool opens).

python3 recut.py <streamer>            every cut clip of that streamer whose banner no longer matches config
python3 recut.py <clip.json> [...]     just these clips
Skips clips already posted. Reuses work/<peak>/clip.ts, so no re-judging.
"""
import json, subprocess, sys
from pathlib import Path

from highlight import CFG, HERE, ROOT, log, vertical
from views import banner_brand, read_ledger

SEARCH = [Path(CFG["live"]), Path(CFG["archive"]), ROOT]


def work_dir(rec):
    for base in SEARCH:
        hits = list(base.glob(f"{rec['streamer']}/*/work/{rec['peak']}"))
        if hits:
            return hits[0]


def recut(cj):
    rec = json.loads(cj.read_text())
    sc = CFG["streamers"][rec["streamer"]]
    logo = banner_brand(sc)
    if rec.get("banner") == logo:
        return "already matches"
    if (read_ledger()["clips"].get(rec["file"]) or {}).get("posts"):
        return "posted, left alone"
    w = work_dir(rec)
    if not w or not (w / "clip.ts").exists() or not (w / "clip.json").exists():
        return "work files missing"
    spec = json.loads((w / "clip.json").read_text())
    split = rec.get("layout") == "facecam-split"
    kick = sc["url"] if logo else None
    for k in ("kick", "logo", "hook_y", "kick_y", "chat_y", "chat_h"):
        spec.pop(k, None)
    spec.update({"hook_y": 730, "kick_y": 600} if split and kick else {"hook_y": 520} if split else {"hook_y": 300, "kick_y": 160} if kick else {})
    spec.update({"chat_y": 1000, "chat_h": 220} if split and kick else {"chat_y": 790, "chat_h": 420} if split else {"chat_y": 620, "chat_h": 440})
    if kick:
        spec.update(kick=kick, logo=f"assets/{logo}-logo.svg")
    (w / "clip.json").write_text(json.dumps(spec))
    r = subprocess.run([sys.executable, str(HERE / "overlay" / "render_overlay.py"), str(w / "clip.json"), str(w / "overlay.webm")],
                       capture_output=True, text=True)
    if r.returncode:
        return "overlay failed " + r.stderr[-200:]
    vertical(w / "clip.ts", w / "overlay.webm", ROOT / "clips" / rec["file"], sc.get("cam"), sc.get("trim_bottom", 0.0), rec.get("focus"))
    rec.update(banner=logo, campaign=sc.get("campaign", rec.get("campaign")))
    cj.write_text(json.dumps(rec, indent=1))
    cv = subprocess.run([sys.executable, str(HERE / "cover.py"), str(cj)], capture_output=True, text=True, timeout=600)
    return "recut" + ("" if cv.returncode == 0 else ", cover failed")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] in CFG["streamers"]:
        files = [p for p in sorted((ROOT / "clips").glob("*/*.json")) if not p.name.endswith(".cover.json")
                 and json.loads(p.read_text()).get("streamer") == args[0] and json.loads(p.read_text()).get("status") == "cut"]
    else:
        files = [Path(a) for a in args]
    for f in files:
        log("recut", f.name, recut(f))
