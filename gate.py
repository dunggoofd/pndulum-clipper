"""Gates a clip must pass before a post goes out. Deterministic, no AI. Built 21 Nov (ops diagnosis fixes 4 + 5).

money(streamer)                 None, or why this streamer earns nothing right now (no campaign, drained, off-niche).
compliance(rec, s, p, caption)  [] or the campaign rules this post would break (tags, #ad, logos, length, on-screen CTA, English).

python3 gate.py                 check every cut clip on the desk against both gates
python3 gate.py <YYYYMMDD/name.mp4>
"""
import json, re, sys, time

from views import CFG, ROOT, read_ledger, safe_text

ALLOC = ROOT / "allocation.json"
ALLOC_FRESH_H = 6
NON_ENGLISH = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af\u0400-\u04ff\u0600-\u06ff]")
MIN_S, MAX_S = 12, 61


def cid(url):
    m = re.search(r"[0-9a-f]{8}-[0-9a-f-]{27}", url or "")
    return m[0] if m else None


def allocation():
    try:
        a = json.loads(ALLOC.read_text())
    except (OSError, ValueError):
        return None
    return a if time.time() - a.get("at", 0) < ALLOC_FRESH_H * 3600 else None


def money(name):
    s = CFG["streamers"].get(name)
    if not s:
        return f"{name} is not in config.yaml"
    if s.get("off_niche") and not s.get("accounts"):
        return f"{name} is off-niche for this account set"
    if not s.get("campaign_url"):
        return f"{name} has no paid campaign: views earn $0"
    a = allocation()
    if a is None:
        return None   # allocator stale or never ran: campaign_url is the best we know
    row = next((r for r in a.get("rows", []) if r.get("key") == cid(s["campaign_url"])), None)
    if row is None:
        return None   # allocator hasn't scored this campaign yet
    if not row.get("joined"):
        return f"{row.get('title', name)} not joined in Content Rewards"
    if row.get("dead"):
        return f"{row.get('title', name)} is drained (${row.get('left', 0)} left)"
    return None


def compliance(rec, s, p, caption):
    j = rec.get("judge") or {}
    hook = j.get("hook") or ""
    cap_l = (caption or "").lower()
    bad = []
    if p not in s.get("post_to", CFG["platforms"]):
        bad.append(f"{p} not allowed for this campaign")
    if rec.get("banner") and (s.get("no_logos") or "reach" in str(s.get("campaign", "")).lower()):
        bad.append(f"carries a {rec['banner']} logo banner, campaign bans logos")
    d = rec.get("duration") or 0
    if not MIN_S <= d <= MAX_S:
        bad.append(f"{d}s long, needs {MIN_S}-{MAX_S}s")
    for t in s.get("tags", []):
        if t.lower() not in cap_l:
            bad.append(f"caption missing required '{t}'")
    if s.get("disclose") and "#ad" not in cap_l.split():
        bad.append("paid campaign, caption needs #ad")
    if s.get("onscreen"):
        want = s["onscreen"].lower()
        if "subscribe" in want and "subscribe" not in (rec.get("cta") or "").lower():
            bad.append("campaign needs an on-screen subscribe CTA and the overlay didn't render one (set `handle:` in config, re-cut)")
    if "peak" in rec and not rec.get("overlay"):
        bad.append("no caption overlay rendered (raw screen-recordings are rejected)")   # auto-cuts only; hand-edited clips carry burned-in captions
    if safe_text(hook) is None or safe_text(j.get("caption") or "") is None:
        bad.append("hook or caption contains an unfixable word")
    if NON_ENGLISH.search(hook + " " + (caption or "")):
        bad.append("non-English text in hook or caption")
    if not hook:
        bad.append("no hook")
    return bad


def check(f):
    rec = json.loads((ROOT / "clips" / f).with_suffix(".json").read_text())
    s = CFG["streamers"].get(rec.get("streamer"), {})
    import publish
    out = {"money": money(rec.get("streamer")), "platforms": {}}
    for p in s.get("post_to", CFG["platforms"]):
        out["platforms"][p] = compliance(rec, s, p, publish.caption(rec, s, p))
    return out


def main():
    files = sys.argv[1:] or [f for f, c in read_ledger()["clips"].items() if c.get("stage") != "rejected" and (ROOT / "clips" / f).with_suffix(".json").exists()]
    for f in files:
        try:
            r = check(f)
        except Exception as e:
            print(f"{f}: {e}")
            continue
        flat = sorted({b for bs in r["platforms"].values() for b in bs})
        print(f"{f}\n  money: {r['money'] or 'ok'}\n  rules: {'ok' if not flat else '; '.join(flat)}")


if __name__ == "__main__":
    main()
