"""Ramp Governor: posts approved clips one platform at a time, inside the warm-up ramp, and pauses a platform that stalls.

Each run posts at most ONE (clip, platform), so platforms stagger naturally. Order: allocator target rank, judge score, newest.
Gate per platform: not paused, under today's ramp cap (governor.ramp by account age, capped by daily_cap), and
min_gap_hours since the last post there. publish.py calls gate() too, so Desk "Post to all" obeys the same rules.
Stall: the last stall_streak posts on a platform (made since governor.since, each stall_after_hours old) all under
stall_views views pauses that platform for pause_hours, with a Telegram alert. One slow post on a new account is
normal; several in a row is the throttle sign. Blotato errors that look like a block also pause.
Only clips for a joined paid campaign are queued (GTA 6, allocator.alist, paid), plus allocator.views_only
streamers (Speed, 22 Nov), which queue last so they only use ramp slots paid clips leave empty.

python3 governor.py [--dry]     (unit clipper-governor, every 15 min)
python3 governor.py --status
"""
import json, re, subprocess, sys, time
from datetime import date

from views import CFG, HERE, ROOT, fetch, ledger, read_ledger

G = CFG["governor"]
STATE = ROOT / "governor.json"
BLOCK = re.compile(r"action blocked|spam|suspend|restricted|too many|rate.?limit|try again later", re.I)


def state():
    return json.loads(STATE.read_text()) if STATE.exists() else {"paused": {}}


def save(st):
    STATE.write_text(json.dumps(st, indent=1))


def day_no():
    return (date.today() - date.fromisoformat(str(G["accounts_created"]))).days + 1


def cap(p):
    c = max([n for d, n in G["ramp"] if day_no() >= d] or [0])
    return min(c, (CFG.get("daily_cap") or {}).get(p, c))


def posts(p):
    return sorted((x for c in read_ledger()["clips"].values() for q, x in c.get("posts", {}).items() if q == p),
                  key=lambda x: x.get("posted") or 0)


def gate(p):
    """None if platform p may post now, else the reason."""
    now = time.time()
    until = state()["paused"].get(p, {}).get("until", 0)
    if until > now:
        return f"paused until {time.strftime('%a %H:%M', time.localtime(until))}: {state()['paused'][p]['why']}"
    ps = posts(p)
    n = sum(1 for x in ps if now - (x.get("posted") or 0) < 86400)
    if n >= cap(p):
        return f"ramp cap {cap(p)}/day on account day {day_no()} reached ({n} in last 24h)"
    if ps and now - (ps[-1].get("posted") or 0) < G["min_gap_hours"] * 3600:
        nxt = ps[-1]["posted"] + G["min_gap_hours"] * 3600
        return f"spacing: next {p} slot {time.strftime('%H:%M', time.localtime(nxt))}"
    return None


def pause(st, p, why):
    if st["paused"].get(p, {}).get("until", 0) > time.time():
        return
    st["paused"][p] = {"until": time.time() + G["pause_hours"] * 3600, "why": why, "at": time.time()}
    import scout
    scout.notify(f"Governor paused {p} for {G['pause_hours']}h: {why}\nPost 1 by hand from the phone when it resumes, "
                 f"then let it ramp again. Desk: http://127.0.0.1:{CFG['dash_port']}")


def check_stalls(st):
    since = time.mktime(time.strptime(str(G["since"]), "%Y-%m-%d"))
    now, by = time.time(), {}
    clips = read_ledger()["clips"]
    for f, c in clips.items():
        for p, x in c.get("posts", {}).items():
            age = now - (x.get("posted") or now)
            if (x.get("posted") or 0) < since or age < G["stall_after_hours"] * 3600:
                continue
            if age < 24 * 3600 and now - (x.get("checked") or 0) > 1800:
                try:
                    v = fetch(x["url"])
                    with ledger() as d:
                        y = d["clips"][f]["posts"][p]
                        y.update(views=v, checked=now, source="auto", error=None)
                        y.setdefault("history", []).append([int(now), v])
                    x["views"] = v
                except Exception:
                    pass
            by.setdefault(p, []).append((x["posted"], x.get("views") or 0, f))
    for p, xs in by.items():
        last = sorted(xs)[-G["stall_streak"]:]
        resumed = st["paused"].get(p, {}).get("until", 0)
        if len(last) == G["stall_streak"] and last[0][0] > resumed and all(v < G["stall_views"] for _, v, _ in last):
            pause(st, p, f"last {len(last)} posts under {G['stall_views']} views after {G['stall_after_hours']}h+ "
                         f"({', '.join(str(v) for _, v, _ in last)})")
    for f, c in clips.items():
        for p, pub in c.get("publish", {}).items():
            if pub.get("state") == "failed" and BLOCK.search(pub.get("error") or "") and now - pub.get("at", 0) < 3600:
                pause(st, p, f"Blotato: {pub['error'][:120]}")


def queue():
    alloc = ROOT / "allocation.json"
    a = json.loads(alloc.read_text()) if alloc.exists() else {"targets": []}
    rank = {k: i for i, k in enumerate(a["targets"])}
    cut = time.time() - G["max_clip_age_hours"] * 3600
    out = []
    for f, c in read_ledger()["clips"].items():
        if c.get("stage") != "approved":
            continue
        meta = ROOT / "clips" / f.replace(".mp4", ".json")
        if not meta.exists():
            continue
        r = json.loads(meta.read_text())
        s = CFG["streamers"].get(r["streamer"], {})
        views_only = r["streamer"] in CFG["allocator"].get("views_only", [])
        if s.get("off_niche") or not (s.get("campaign_url") or views_only) or r.get("peak", 0) < cut:
            continue
        m = re.search(r"[0-9a-f]{8}-[0-9a-f-]{27}", s.get("campaign_url") or "")
        rk = rank.get(m[0] if m else None, 99) if not views_only else 100
        for p in s.get("post_to", CFG["platforms"]):
            busy = c.get("publish", {}).get(p, {}).get("state") in ("uploading", "publishing", "submitting")
            if p not in c.get("posts", {}) and not busy:
                out.append((rk, -r["judge"].get("score", 0), -r.get("peak", 0), f, p))
    return sorted(out)


def main():
    if "--status" in sys.argv:
        print(f"account day {day_no()}")
        for p in CFG["platforms"]:
            print(f"{p:10} cap {cap(p)}/day  {gate(p) or 'open'}")
        for q in queue()[:10]:
            print("queued", q[3], q[4], f"target#{q[0] + 1}" if q[0] < 99 else "views-only" if q[0] == 100 else "untargeted")
        return
    st = state()
    check_stalls(st)
    save(st)
    for rk, _, _, f, p in queue():
        if gate(p):
            continue
        print(time.strftime("%H:%M"), "post", p, f, flush=True)
        if "--dry" in sys.argv:
            return
        subprocess.run([sys.executable, str(HERE / "publish.py"), f, "--platforms", p], cwd=HERE, timeout=1800)
        return


if __name__ == "__main__":
    main()
