"""Submit a posted clip link to its Content Rewards campaign through the browser hub (Chrome :9333).

python3 submit.py <YYYYMMDD/name.mp4> <platform> [--dry-run]
python3 submit.py --sync          read every Content Rewards submission, mark matching ledger posts submitted
--dry-run fills the dialog and ticks the box but never clicks Submit.

Content Rewards' own submissions list (the JSON the Submissions page loads) is the source of truth:
checked before the dialog opens (never submit a post that is already there) and after Submit (success = our post id shows up).
"""
import argparse, json, re, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from views import CFG, HERE, ROOT, ledger, read_ledger, no_logos, safe_text
from publish import caption

HUB = HERE / "browser-hub" / "hub.sh"
SHOTS = HERE / "logs" / "submit"
AUTOPOST = ROOT / "autopost.json"
API = "https://contentrewards.com/api/submission/submissions?limit=50&creatorView=true&retainer=false"
CLEAN_TO_REARM = 3


def mark(f, p, **kw):
    with ledger() as d:
        d["clips"].setdefault(f, {}).setdefault("publish", {}).setdefault(p, {}).update(kw, at=time.time())


def norm(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


def post_id(url):
    m = re.search(r"(?:/video/|/status/|/reel/|/p/|/shorts/|[?&]v=|youtu\.be/)([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else url


def cr_submissions(ctx):
    """Every submission on the account, live and deleted, keyed by post id. Live entries win over deleted ones."""
    out = {}
    for q in ("&isDeleted=true", ""):
        cur = None
        while True:
            d = ctx.request.get(API + q + (f"&cursor={quote(cur)}" if cur else "")).json()
            if not d.get("success"):
                raise RuntimeError(f"could not read Content Rewards submissions: {d.get('error')}")
            for it in d["data"]:
                out[post_id((it.get("socialMediaPost") or {}).get("postUrl"))] = it
            nxt = d["pagination"].get("nextCursor")
            if not d["data"] or not nxt or nxt == cur:
                break
            cur = nxt
    return out


def sync(ctx):
    """Mark every ledger post that Content Rewards lists as submitted, with its real time, campaign and review status."""
    subs = cr_submissions(ctx)
    n = 0
    with ledger() as d:
        for f, c in d["clips"].items():
            for p, post in c.get("posts", {}).items():
                it = subs.get(post_id(post.get("url")))
                if not it:
                    continue
                st = "deleted" if it.get("isDeleted") else it.get("status")
                if not post.get("submitted"):
                    n += 1
                post.update(submitted=True, cr_status=st, cr_campaign=it.get("campaignName"), cr_id=it["id"],
                            submitted_at=datetime.fromisoformat(it["createdAt"].replace("Z", "+00:00")).timestamp())
                pub = c.setdefault("publish", {}).setdefault(p, {})
                if pub.get("state") in ("submit-failed", "submitting", "published", None):
                    pub.update(state="submitted", error=None)
    return subs, n


def rearm_autopost():
    """After CLEAN_TO_REARM verified submits, clear the autopost circuit breaker."""
    if not AUTOPOST.exists():
        return
    st = json.loads(AUTOPOST.read_text())
    if not st.get("submit_off"):
        return
    st["clean"] = st.get("clean", 0) + 1
    if st["clean"] >= CLEAN_TO_REARM:
        st = {"rearmed_at": time.time()}
        print("autopost auto-submit back ON after", CLEAN_TO_REARM, "verified submits", flush=True)
    AUTOPOST.write_text(json.dumps(st, indent=1))


def pick_clip(page, dlg, rec, s, p, url):
    """Select exactly our post in the linked-account picker (Content Rewards dialog, Nov 2026). X pastes the link."""
    if p == "x":
        dlg.get_by_role("textbox", name="Clip link").fill(url, timeout=15000)
        return "pasted"
    want = norm(safe_text(rec["judge"]["hook"]) if p == "youtube" else caption(rec, s, p))[:28]
    btns = dlg.get_by_role("region", name=re.compile("New clips")).get_by_role("button")
    n = btns.count()
    names = [btns.nth(i).get_attribute("aria-label") or btns.nth(i).inner_text() for i in range(n)]
    hits = [i for i, nm in enumerate(names) if want and norm(nm).startswith(want)]
    if len(hits) > 1:
        raise RuntimeError(f"{len(hits)} picker clips match this post; submit by hand")
    for i in range(n):
        if (btns.nth(i).get_attribute("aria-pressed") == "true") != (i in hits):
            btns.nth(i).click()
            page.wait_for_timeout(300)
    if [btns.nth(i).get_attribute("aria-pressed") == "true" for i in range(n)] != [i in hits for i in range(n)]:
        raise RuntimeError("picker selection would not settle")
    if hits:
        return "picked"
    more = dlg.get_by_role("button", name=re.compile("can.t be submitted"))
    if more.count():
        more.first.click()
        page.wait_for_timeout(1000)
        paste = dlg.get_by_role("button", name="Paste link")
        if paste.count():
            paste.first.click()
            page.wait_for_timeout(1000)
            dlg.get_by_role("textbox").last.fill(url, timeout=15000)
            return "pasted (account not loading)"
    raise RuntimeError("our post is not in the picker (account not linked in Content Rewards, already submitted, or past 30 min)")


def hub():
    tabs = subprocess.run(["curl", "-s", "-m", "5", "http://127.0.0.1:9333/json/list"], capture_output=True, text=True).stdout
    if '"type": "page"' not in tabs:
        subprocess.run(["curl", "-s", "-m", "10", "-X", "PUT", "http://127.0.0.1:9333/json/new?about:blank"], capture_output=True)


def sync_cli():
    subprocess.run([str(HUB), "lock", "contentrewards", "sync", "120"], check=True, capture_output=True)
    try:
        hub()
        with sync_playwright() as pw:
            subs, n = sync(pw.chromium.connect_over_cdp("http://127.0.0.1:9333").contexts[0])
        print(f"content rewards: {len(subs)} submissions, {n} newly marked submitted", flush=True)
        return subs
    finally:
        subprocess.run([str(HUB), "unlock", "contentrewards"], capture_output=True)


def run(f, p, dry=False, camp=None, force=False):
    rec = json.loads((ROOT / "clips" / f).with_suffix(".json").read_text())
    s = CFG["streamers"][rec["streamer"]]
    camp = camp or s.get("campaign_url")
    if rec.get("banner") and no_logos(s):
        msg = f"clip carries a {rec['banner']} logo banner and this campaign bans logos (Reach); re-cut it without the banner"
        mark(f, p, state="submit-failed", error=msg)
        raise SystemExit(msg)
    post = read_ledger()["clips"].get(f, {}).get("posts", {}).get(p)
    if not camp or not post:
        raise SystemExit("no campaign_url for this streamer, or no posted link for this platform")
    if post.get("submitted"):
        raise SystemExit("already marked submitted, not submitting again")
    pid = post_id(post["url"])
    SHOTS.mkdir(parents=True, exist_ok=True)
    shot = SHOTS / f"{Path(f).stem}-{p}-{int(time.time())}.png"
    subprocess.run([str(HUB), "lock", "contentrewards", f"submit-{p}", "300"], check=True, capture_output=True)
    try:
        hub()
        with sync_playwright() as pw:
            ctx = pw.chromium.connect_over_cdp("http://127.0.0.1:9333").contexts[0]
            subs, _ = sync(ctx)
            if pid in subs:
                it = subs[pid]
                print(f"already in Content Rewards ({it.get('campaignName')}, {it.get('status')}), not submitting", flush=True)
                return
            clicked = read_ledger()["clips"][f].get("publish", {}).get(p, {}).get("clicked_at")
            if clicked and time.time() - clicked < 180 and not dry and not force:
                raise RuntimeError("Submit was clicked under 3 min ago and Content Rewards doesn't list it yet; wait, then retry")
            mark(f, p, state="submitting", error=None)
            page = ctx.new_page()
            try:
                page.goto(camp, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                nn = page.get_by_role("button", name="Not now")
                if nn.count():
                    nn.first.click()
                    page.wait_for_timeout(800)
                page.get_by_role("button", name="Submit clip").first.click(timeout=30000)
                dlg = page.get_by_role("dialog", name="Submit clip")
                dlg.wait_for(timeout=15000)
                page.wait_for_timeout(2500)
                how = pick_clip(page, dlg, rec, s, p, post["url"])
                page.wait_for_timeout(2000)
                box = dlg.get_by_role("checkbox")
                if not box.is_checked():
                    dlg.get_by_text("I've read the requirements").click()
                if not box.is_checked():
                    raise RuntimeError("requirements box would not tick")
                btn = dlg.get_by_role("button", name="Submit clip", exact=True)
                if dry:
                    page.screenshot(path=str(shot))
                    mark(f, p, state="published", note=f"dry run ok ({how}), Submit {'enabled' if btn.is_enabled() else 'DISABLED'}")
                    print("dry run", how, "submit enabled" if btn.is_enabled() else "submit disabled", shot)
                    return
                if not btn.is_enabled():
                    raise RuntimeError(f"Submit stayed disabled: {dlg.inner_text()[:200]}")
                base = set(dlg.inner_text().splitlines())
                before = set(subs)
                mark(f, p, clicked_at=time.time())
                btn.click()
                text, found = "", False
                for i in range(1, 61):
                    page.wait_for_timeout(1000)
                    if dlg.is_visible():
                        text = " ".join(l for l in dlg.inner_text().splitlines() if l not in base)
                    if i % 5 == 0:
                        subs = cr_submissions(ctx)
                        if pid in subs:
                            found = True
                            break
                page.screenshot(path=str(shot))
                sync(ctx)
                if found:
                    mark(f, p, state="submitted", shot=str(shot))
                    rearm_autopost()
                    print("submitted", p, post["url"], flush=True)
                    return
                other = [subs[k] for k in set(subs) - before]
                if other:
                    sp = other[0].get("socialMediaPost") or {}
                    raise RuntimeError(f"picker submitted a different post ({sp.get('platform')} {sp.get('postUrl')}), not ours")
                said = re.sub(r"\s+", " ", text).strip()
                raise RuntimeError(("Content Rewards said: " + said[:240]) if said else "not in Content Rewards 60s after Submit")
            finally:
                page.close()
    except Exception as e:
        mark(f, p, state="submit-failed", error=str(e)[:300], shot=str(shot) if shot.exists() else None)
        print("submit failed", p, e, file=sys.stderr, flush=True)
    finally:
        subprocess.run([str(HUB), "unlock", "contentrewards"], capture_output=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?")
    ap.add_argument("platform", nargs="?")
    ap.add_argument("--sync", action="store_true", help="only sync the ledger from Content Rewards' submissions list")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--campaign-url", help="submit to another campaign, e.g. a streamer's alt_campaign_url")
    ap.add_argument("--force", action="store_true", help="skip the 3-minute wait after a recent Submit click")
    a = ap.parse_args()
    if a.sync:
        sync_cli()
    else:
        run(a.file, a.platform, a.dry_run, a.campaign_url, a.force)
