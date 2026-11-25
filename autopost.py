"""Autopost: approves and posts every clip the judge scored 8+, bypassing the ramp governor (Dung's call, 20 Nov).

Posts via publish.py --force (no governor gate, no ramp, no daily_cap), then publish.py submits to the campaign
for streamers that have one. One CLIP per run to every platform it still needs (21 Nov: platform-by-platform spacing bought
nothing and left one clip 90 min from first to last platform; the 20-30 min random gap now sits between clips). Runs never overlap. Skips off_niche streamers, clips older than
governor.max_clip_age_hours, and bannered clips for no-logos campaigns (submit.py would refuse them).
Circuit breaker: the first submit-failed stops all further submits (posts continue) until autopost.json is cleared.

python3 autopost.py [--dry]     (unit clipper-autopost, every 5 min)
"""
import fcntl, json, random, subprocess, sys, time

import gate
from views import CFG, HERE, ROOT, ledger, no_logos, read_ledger

MIN_SCORE = 8
STATE = ROOT / "autopost.json"
GAP = ROOT / "autopost-gap.json"
GAP_MIN = (20, 30)
BUSY = {"uploading", "publishing", "submitting", "failed", "submit-failed"}


def state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def queue():
    cut = time.time() - CFG["governor"]["max_clip_age_hours"] * 3600
    out = []
    for f, c in read_ledger()["clips"].items():
        if c.get("stage") == "rejected":
            continue
        meta = ROOT / "clips" / f.replace(".mp4", ".json")
        if not meta.exists():
            continue
        r = json.loads(meta.read_text())
        j = r.get("judge") or {}
        s = CFG["streamers"].get(r["streamer"])
        if not s or s.get("off_niche") or not j.get("keep") or j.get("score", 0) < MIN_SCORE or r.get("peak", 0) < cut:
            continue
        if r.get("banner") and no_logos(s):
            continue
        if gate.money(r["streamer"]):
            continue   # no live paid campaign: the governor may still warm the account with it, autopost never will
        plats = [p for p in s.get("post_to", CFG["platforms"])
                 if p not in c.get("posts", {}) and c.get("publish", {}).get(p, {}).get("state") not in BUSY]
        if plats:
            out.append((-j["score"], -r.get("peak", 0), f, plats, r["streamer"], j["score"]))
    return sorted(out)


def main():
    lock = open(ROOT / "autopost.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    q = queue()
    nxt = json.loads(GAP.read_text())["next_at"] if GAP.exists() else 0
    if not q or time.time() < nxt:
        return
    _, _, f, plats, who, score = q[0]
    print(time.strftime("%H:%M"), "autopost", who, score, f, plats, flush=True)
    if "--dry" in sys.argv:
        for x in q:
            print("queued", x[2], x[3], x[4], x[5])
        return
    if gate.money(who):
        return
    with ledger() as d:
        c = d["clips"].setdefault(f, {})
        if c.get("stage") != "approved":
            c["stage"] = "approved"
            c["auto_approved"] = time.time()
    st = state()
    cmd = [sys.executable, str(HERE / "publish.py"), f, "--platforms", ",".join(plats), "--force"]
    if st.get("submit_off"):
        cmd.append("--no-submit")
    subprocess.run(cmd, cwd=HERE, timeout=3600)
    GAP.write_text(json.dumps({"next_at": time.time() + random.uniform(*GAP_MIN) * 60}))
    pub = read_ledger()["clips"].get(f, {}).get("publish", {})
    res = {p: pub.get(p, {}).get("state", "?") for p in plats}
    bad = [p for p, s in res.items() if s == "submit-failed"]
    if bad and not st.get("submit_off"):
        STATE.write_text(json.dumps({"submit_off": True, "why": pub[bad[0]].get("error"), "clip": f, "at": time.time()}, indent=1))
    try:
        import scout
        msg = f"Autopost {who} score {score}: " + ", ".join(f"{p} {s}" for p, s in res.items())
        if bad and not st.get("submit_off"):
            msg += f"\nSubmit failed, auto-submit now OFF: {pub[bad[0]].get('error')}"
        scout.notify(msg)
    except Exception as e:
        print("notify failed", e, flush=True)


if __name__ == "__main__":
    main()
