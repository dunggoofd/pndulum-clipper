"""Pull public view counts for every posted clip URL in the ledger. yt-dlp first, Blotato analytics when it has none (IG, X).

python3 views.py     refresh all posts (unit clipper-views, every 3h)
"""
import fcntl, json, os, re, subprocess, time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).parent
CFG = __import__("cfg").load()


SWAPS = [
    (r"road ?kills?", "unalived critter"), (r"kill ?streaks?", "unalive streak"), (r"kill ?cams?", "unalive cam"),
    (r"kill ?feeds?", "unalive feed"), (r"overkill", "way too much"), (r"kill ?counts?", "unalive count"),
    (r"kill(s|ed|ing)?", lambda m: "unaliv" + {"": "e", "s": "es", "ed": "ed", "ing": "ing"}.get((m.group(1) or "").lower(), "")),
    (r"murder(s|ed|ing)?", lambda m: "unaliv" + {"": "e", "s": "es", "ed": "ed", "ing": "ing"}.get((m.group(1) or "").lower(), "")),
    (r"killers?|murderers?|shooters?|gunman|gunmen|terrorists?", "menace"),
    (r"kill\w*", "unalived"),
    (r"suicide|kys", "unalive"),
    (r"dead|die|dies|died|dying|death", "cooked"),
    (r"shoot|shoots|shooting", "pew pew"), (r"shot", "hit"),
    (r"spear ?guns?", "harpoon"),
    (r"guns?|pistols?|rifles?|shotguns?|ak-?47s?|weapons?", "pew pew"),
    (r"gunfights?|shootouts?|gunfire", "showdown"),
    (r"blood(y)?", "ketchup"), (r"stab(s|bed|bing)?", "poke"), (r"bombs?", "boom"), (r"bombed", "blew up"),
    (r"kidnap(s|ped|ping)?", "snatch"), (r"hostages?", "captive"),
    (r"sex", "seggs"), (r"sexy", "spicy"), (r"porn", "corn"), (r"nude|naked", "unclothed"),
    (r"lap ?dances?", "dance"), (r"strippers?", "dancer"), (r"strip ?club", "club"), (r"hookers?|prostitutes?|escorts?", "date"),
    (r"drugs?|cocaine|meth|heroin|crack|weed|fentanyl", "contraband"), (r"drug dealers?|dealers?", "plug"),
    (r"fuck(ing|ed)?", "clapped"), (r"wtf", "wth"), (r"shit", "crap"), (r"bitch(es)?", "bihh"),
]
SWAPS = [(re.compile(rf"\b(?:{p})\b", re.I), r) for p, r in SWAPS]
HARD = re.compile(r"\brap(e|ed|ist)\b|\bnazis?\b|\bhitler\b|\bself[- ]?harm\b|\bcut (myself|himself)\b|n[i1!|]gg|f[a@4]gg|ret[a@4]rd|\bk[i1]ke|tr[a@4]nn(y|ie)|\bchinks?\b|\bspics?\b", re.I)
BAD_EMOJI = re.compile("[\U0001F52B\U0001F48A\U0001F489\U0001F346\U0001F351\U0001F4A6\U0001FA78\U0001F52A\U0001F4A3\U0001F6AC]️?")


def _case(src, rep):
    return rep.upper() if src.isupper() else rep[:1].upper() + rep[1:] if src[:1].isupper() else rep


def safe_text(s):
    """Swap words that get clips suppressed on TikTok/IG/YouTube/X. None if unfixable (slur, sexual violence)."""
    if not s or HARD.search(s):
        return None if s else s
    for rx, rep in SWAPS:
        s = rx.sub(lambda m: _case(m.group(0), rep(m) if callable(rep) else rep), s)
    return re.sub(r"\s{2,}", " ", BAD_EMOJI.sub("", s)).strip()


TAG_ROOTS = re.compile(r"kill|murder|suicide|unalive|dead|death|shoot|gun|blood|bomb|sex|porn|nude|naked|strip|hooker|drug|cocaine|meth|weed|fuck|shit|bitch", re.I)


def safe_tags(tags):
    return [t for t in tags if not TAG_ROOTS.search(t) and safe_text(t) == t]


def no_logos(s):
    return bool(s.get("no_logos")) or "reach" in str(s.get("campaign", "")).lower()


def banner_brand(s):
    if no_logos(s):
        return None
    return s.get("banner") or ("kick" if s.get("platform") == "kick" else None)
ROOT = Path(CFG["root"])
LEDGER = ROOT / "ledger.json"


@contextmanager
def ledger():
    with open(ROOT / ".ledger.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        data = json.loads(LEDGER.read_text()) if LEDGER.exists() else {"clips": {}, "rates": {}}
        yield data
        tmp = LEDGER.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(LEDGER)


def read_ledger():
    return json.loads(LEDGER.read_text()) if LEDGER.exists() else {"clips": {}, "rates": {}}


def allowed(name):
    """Allocator's recording list. Off-niche streamers never; stale or missing allocation (>6h) allows the rest."""
    if CFG["streamers"].get(name, {}).get("off_niche"):
        return False
    f = ROOT / "allocation.json"
    try:
        a = json.loads(f.read_text())
    except (OSError, ValueError):
        return True
    return time.time() - a["at"] > 6 * 3600 or name in a["record"]


def fetch(url):
    r = subprocess.run(["yt-dlp", "-j", "--skip-download", "--no-warnings", "--no-playlist", url],
                       capture_output=True, text=True, timeout=90)
    if r.returncode:
        raise RuntimeError((r.stderr.strip().splitlines() or ["yt-dlp failed"])[-1][:200])
    v = json.loads(r.stdout).get("view_count")
    if v is None:
        raise RuntimeError("no public view count")
    return int(v)


def norm(url):
    return url.split("?")[0].rstrip("/").lower().replace("twitter.com", "x.com").replace("www.", "")


def blotato_views(since):
    """Published Blotato posts -> {url: views}. Blotato syncs stats in the background, about a day behind."""
    import publish
    out, q = {}, {"status": "published", "limit": 250, "since": datetime.utcfromtimestamp(since).strftime("%Y-%m-%dT%H:%M:%SZ")}
    while True:
        d = publish.call("GET", "/posts", params=q)
        for i in d["items"]:
            m = ((i["state"].get("analytics") or {}).get("latest") or {}).get("metrics") or {}
            if i["state"].get("postUrl") and m.get("viewsCount") is not None:
                out[norm(i["state"]["postUrl"])] = int(m["viewsCount"])
        if not d.get("cursor"):
            return out
        q["cursor"] = d["cursor"]


def main():
    clips = read_ledger()["clips"]
    jobs = [(f, p, post["url"]) for f, c in clips.items() for p, post in c.get("posts", {}).items()]
    try:
        bl = blotato_views(min((post.get("posted") or time.time() for c in clips.values() for post in c.get("posts", {}).values()), default=time.time()) - 86400)
    except Exception as e:
        bl = {}
        print("blotato:", e, flush=True)
    for f, p, url in jobs:
        src = "auto"
        try:
            views, err = fetch(url), None
        except Exception as e:
            views, err = bl.get(norm(url)), None
            if views is None:
                err = f"{e}; Blotato has no stats yet (syncs about a day after posting)"
            else:
                src = "blotato"
        with ledger() as d:
            post = d["clips"].get(f, {}).get("posts", {}).get(p)
            if not post or post["url"] != url:
                continue
            post["checked"] = time.time()
            post["error"] = err
            if views is not None:
                post["views"] = views
                post["source"] = src
                post.setdefault("history", []).append([int(time.time()), views])
        print(datetime.now().strftime("%H:%M:%S"), p, f, views if views is not None else err, flush=True)
    subs = None
    try:
        import submit
        subs = submit.sync_cli()
    except Exception as e:
        print("content rewards sync failed:", e, flush=True)
    try:
        import sentinel
        sentinel.main()
    except Exception as e:
        print("sentinel failed:", e, flush=True)


if __name__ == "__main__":
    main()
