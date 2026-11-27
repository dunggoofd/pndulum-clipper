"""Allocator: what each GTA VI campaign really pays us per 1k views, and which 2-3 to aim at.

Reads the public metrics on each Content Rewards campaign page and writes ~/gta-clipper/allocation.json:
  record   streamers rec.py may record (joined, paid, GTA, A-list, not drained). Everyone else idles.
  targets  campaign keys governor.py posts first, best first.
Pools pay only creators whose own approved views pass minCreatorViews (inferred 19 Nov from page data: Clix and Kai
pools show no shares while every clipper is under the floor; Lacy shows shares once leaders passed it), so a pool is worth nothing to us until we can reach that floor.

python3 allocator.py [--dry]   (unit clipper-allocator, hourly)
"""
import calendar, json, re, statistics, sys, time
from datetime import date
from pathlib import Path

import scout
from views import CFG, ROOT, read_ledger

OUT = ROOT / "allocation.json"
FEE = 0.9
A = CFG["allocator"]
G = CFG["governor"]


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def cid(url):
    m = re.search(r"[0-9a-f]{8}-[0-9a-f-]{27}", url or "")
    return m[0] if m else None


def page(c):
    t = scout.get(f"{scout.CR}/c/campaigns/{c}").replace('\\"', '"')
    m = re.search(r'"metrics":\{"approvedSubmissionCount":(\d+),"budgetProgressBps":\d+,"budgetSpentCents":(\d+),"creatorCount":(\d+),'
                  r'"paidOutCents":\d+,(?:"poolSplit":(\{.*?\}),)?"totalViews":(\d+)\}', t)
    if not m:
        raise RuntimeError("no metrics block on campaign page")
    num = lambda k: int(x[1]) if (x := re.search(rf'"{k}":(\d+)', t)) else None
    name = re.search(r'"name":"([^"]+)"', t[m.end():m.end() + 400])
    launch = re.search(r'"launchDate":"([^"]+)"', t)
    top = re.search(r'"topEarners":\[(.*?)\],"totalViews"', t)
    rates = [(int(r) / 100, int(mn) / 100, int(mx) / 100) for mx, mn, p, r in
             re.findall(r'\{"maxPayout":(\d+),"minPayout":(\d+),"platform":"(\w+)","rate":(\d+)\}', t)]
    return dict(name=(name[1] if name else c).replace("$$", "$"), spent=int(m[2]) / 100, clippers=int(m[3]),
                split=json.loads(m[4]) if m[4] else None, total=int(m[5]), budget=(num("budgetCents") or 0) / 100,
                floor=num("minCreatorViews"), clip_floor=num("minClipViews"), rates=rates,
                leaders=[int(v) for v in re.findall(r'"totalViews":(\d+)', top[1])] if top else [],
                launched=calendar.timegm(time.strptime(launch[1][:19], "%Y-%m-%dT%H:%M:%S")) if launch else None)


def our_views():
    """Median views per post aged 24h+ (prior until 5 exist) and approved views per streamer so far."""
    now, aged, per = time.time(), [], {}
    for f, c in read_ledger()["clips"].items():
        s = f.split("/")[1].split("-")[0]
        for p in c.get("posts", {}).values():
            if p.get("views") is None:
                continue   # IG/X with no stats yet are unknown, not zero (21 Nov: they dragged the median to 2.5)
            v = p["views"]
            if now - (p.get("posted") or now) >= 86400:
                aged.append(v)
            if p.get("submitted"):
                per[s] = per.get(s, 0) + v
    return (statistics.median(aged) if len(aged) >= 5 else A["prior_views_per_clip"]), per


def slots_left():
    created = date.fromisoformat(str(G["accounts_created"]))
    end = date.fromisoformat(str(A["deadline"]))
    n = 0
    for d in range((date.today() - created).days + 1, (end - created).days + 2):
        cap = max([c for day, c in G["ramp"] if d >= day] or [0])
        n += cap * len(CFG["platforms"])
    return n


def score(c, p, v_clip, ours, slots):
    now = time.time()
    left = max(0.0, p["budget"] - p["spent"])
    days_live = max(0.25, (now - (p["launched"] or now - 86400)) / 86400)
    if p["rates"]:
        rate, mn, mx = max(p["rates"])
        net = rate * FEE
        clip_floor = mn / rate * 1000 if rate else 0
        burn = p["spent"] / days_live
        runway = left / burn if burn else 99
        reach = min(1.0, v_clip / clip_floor) if clip_floor else 1.0
        s = net * reach * min(1.0, runway / 3)
        why = (f"${rate:g}/1k (${net:.2f} net), first $ at {clip_floor:,.0f} views on one clip, cap ${mx:g}/clip, "
               f"${left:,.0f} left, ~{runway:.1f} days of budget at current burn")
        dead = left < 50
        return dict(kind="cpm", net_per_1k=round(net, 2), first_dollar_views=round(clip_floor), score=round(s, 3),
                    left=round(left), runway_days=round(runway, 1), why=why, dead=dead)
    pool, floor = p["budget"], p["floor"] or 0
    bps = (p["split"] or {}).get("topShareBps") or []
    qualified = p["leaders"][0] / (bps[0] / 10000) if bps and p["leaders"] else 0
    eff = pool * FEE * 1000 / (qualified + max(floor, 1))
    have = ours.get(c["streamer"], 0)
    projected = have + v_clip * slots / max(1, A["targets"])
    reach = min(1.0, projected / floor) if floor else 1.0
    s = eff * reach ** 2
    lead = p["leaders"][0] if p["leaders"] else 0
    why = (f"${pool:,.0f} pool, pays only past {floor:,} of OUR approved views (we have {have:,}, "
           f"projected {projected:,.0f} by {A['deadline']}), leader at {lead:,}, "
           f"{'nobody qualified yet' if not bps else f'~{qualified / 1e6:.1f}M qualified views'}, "
           f"worth ~${eff:.2f}/1k net once we qualify")
    return dict(kind="pool", net_per_1k=round(eff, 2), first_dollar_views=floor, score=round(s, 3), left=round(pool),
                our_views=have, projected=round(projected), leader=lead, why=why, dead=False)


def campaigns():
    alist = {norm(n) for n in A["alist"]}
    out = {}
    for name, s in CFG["streamers"].items():
        if s.get("off_niche"):
            continue
        for k in ("campaign_url", "alt_campaign_url"):
            c = cid(s.get(k))
            if c and c in out:
                out[c]["streamers"].append(name)   # one campaign, several streamers (sponsor logo campaigns like ClipUp)
            elif c:
                out[c] = dict(key=c, streamer=name, streamers=[name], joined=True, alt=k == "alt_campaign_url", url=s[k])
    latest = ROOT / "scout" / "latest.json"
    for r in (json.loads(latest.read_text())["rows"] if latest.exists() else []):
        c = cid(r.get("url"))
        if c and c not in out and r.get("lane") == "gta" and norm(r.get("streamer")) in alist:
            out[c] = dict(key=c, streamer=r["streamer"], joined=False, alt=False, url=r.get("reach_url") or r["url"])
    return out


def main():
    dry = "--dry" in sys.argv
    v_clip, ours = our_views()
    slots = slots_left()
    rows = []
    for c in campaigns().values():
        try:
            p = page(c["key"])
        except Exception as e:
            print(f"skip {c['streamer']}: {e}")
            continue
        rows.append(dict(c, title=p["name"], clippers=p["clippers"], **score(c, p, v_clip, ours, slots)))
        time.sleep(1)
    rows.sort(key=lambda r: -r["score"])
    live = [r for r in rows if r["joined"] and not r["dead"] and not r["alt"]]
    targets = [r["key"] for r in live if r["score"] > 0][:A["targets"]]
    if len(targets) < A["targets"]:
        # Little or nothing scores while our median views sit under every floor. Idling recorders guarantees $0, so fill
        # the slots with the joined $/1k campaigns with budget left (best net rate first), then the pools we have views in.
        cpm = sorted((r for r in live if r["kind"] == "cpm"), key=lambda r: -r["net_per_1k"])
        pools = sorted((r for r in live if r["kind"] == "pool"), key=lambda r: -r.get("our_views", 0))
        for r in cpm + pools:
            if len(targets) >= A["targets"]:
                break
            if r["key"] not in targets:
                targets.append(r["key"])
    record = sorted({n for r in live if r["key"] in targets for n in r.get("streamers", [r["streamer"]]) if not CFG["streamers"][n].get("vod_only")}
                    | {n for n in A.get("views_only", []) if n in CFG["streamers"] and not CFG["streamers"][n].get("off_niche")})
    prev = json.loads(OUT.read_text()) if OUT.exists() else {}
    res = dict(at=time.time(), views_per_clip=v_clip, slots_to_deadline=slots, targets=targets, record=record, rows=rows)
    for r in rows:
        tag = "TARGET" if r["key"] in targets else "JOIN?" if not r["joined"] else "drained" if r["dead"] else "record" if r["streamer"] in record else ""
        print(f"{r['score']:8.3f}  {tag:7} {r['streamer']:13} {r['title'][:42]:42} {r['why']}")
    print(f"record: {', '.join(record)}  | views/clip used: {v_clip:,}  | post slots to {A['deadline']}: {slots}")
    if dry:
        return
    OUT.write_text(json.dumps(res, indent=1))
    if prev.get("targets") != targets or prev.get("record") != record:
        by = {r["key"]: r for r in rows}
        lines = ["Allocator: new plan"] + [f"{i + 1}. {by[k]['streamer']}: {by[k]['why']}" for i, k in enumerate(targets)]
        lines.append(f"Recording only: {', '.join(record)}")
        joins = [r for r in rows if not r["joined"] and r["score"] > (by[targets[-1]]["score"] if targets else 0)]
        lines += [f"Worth joining: {r['streamer']} {r['title']} ({r['url']})" for r in joins[:3]]
        scout.notify("\n".join(lines))


if __name__ == "__main__":
    main()
