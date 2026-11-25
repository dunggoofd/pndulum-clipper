"""Campaign scout: new streamer-clip campaigns on Content Rewards + Reach.

python3 scout.py           pull, classify new campaigns, brief the fits, ping Telegram
python3 scout.py --dry     pull + classify only: no briefs, no Telegram, state still saved
Runs every 3h (unit clipper-scout). State <root>/scout/, briefs <root>/scout/briefs/.
Fit = lane "gta" (any GTA VI streamer/creator campaign) or lane "bigname" (household-name streamer, any content).
Bump CRITERIA when the fit rule changes: the next run re-checks every live campaign once, no ping. Never joins anything.
"""
import html, json, re, subprocess, sys, tempfile, time
from datetime import datetime
from pathlib import Path

import requests, yaml

HERE = Path(__file__).parent
CFG = __import__("cfg").load()
OUT = Path(CFG["root"]) / "scout"
SEEN = OUT / "seen.json"
VAULT = OUT / "briefs"
CR = "https://contentrewards.com"
REACH = "https://reachclipping.com"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131 Safari/537.36"}
CATS = {"Streamer", "Twitch", "Kick", "Gaming", "YouTuber", "Content Creator", "Esports", "Minecraft", "Fan Page",
        "Entertainment", "Celebrity", "Comedy", "IRL"}
KW = re.compile(r"stream|twitch|kick\b|gta|gaming|gamer|fortnite|minecraft|roblox|call of duty|valorant|warzone|esports", re.I)
BRIEFS_PER_RUN = 6
CRITERIA = 2


def get(url):
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return r.text


def rsc_objects(s, key):
    """JSON objects embedded (escaped) in a Next.js RSC payload, starting at {\"<key>\"."""
    out, i, start = [], 0, '{\\"' + key + '\\"'
    while (i := s.find(start, i)) != -1:
        d, j, instr = 0, i, False
        while j < len(s):
            c = s[j]
            if c == '"' and s[j - 1] == "\\" and s[j - 2] != "\\":
                instr = not instr
            elif not instr and c == "{":
                d += 1
            elif not instr and c == "}":
                d -= 1
                if d == 0:
                    break
            j += 1
        try:
            out.append(json.loads(json.loads('"' + s[i:j + 1] + '"')))
        except ValueError:
            pass
        i = j
    return out


def cr_campaigns():
    found = {}
    for page in range(1, 16):
        cards = rsc_objects(get(f"{CR}/c/discover/clipping?page={page}"), "availableBudgetRaw")
        if not cards:
            break
        for c in cards:
            found[c["id"]] = dict(
                key=c["id"], source="Content Rewards", title=c["title"].replace("$$", "$"), brand=c.get("brand") or "",
                category=c.get("category") or "", type=c.get("type"), cpm=c.get("payoutSortRaw") if c.get("type") == "cpm" else None,
                pool=c.get("budgetTotalRaw") if c.get("type") == "pool" else None, budget_total=c.get("budgetTotalRaw"),
                budget_left=round(c.get("availableBudgetRaw") or 0), creators=c.get("creatorCountRaw") or 0,
                platforms=c.get("platforms") or [], application=bool(c.get("requiresApplication")),
                launched=(c.get("launchedAtMs") or 0) / 1000, description=(c.get("description") or "")[:700],
                url=f"{CR}/c/campaigns/{c['id']}")
    return found


def page_text(s):
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def reach_campaigns(cache):
    out = []
    for href, inner in re.findall(r'<a[^>]+href="/([\w-]+)"[^>]*>(.*?)</a>', get(f"{REACH}/campaigns"), flags=re.S):
        t = page_text(inner)
        m = re.search(r"([\d,]+) MVR · \$([\d,]+) budget", t)
        if not m:
            continue
        if href not in cache:
            detail = get(f"{REACH}/{href}")
            cr = re.search(r"contentrewards\.com/c/campaigns/([0-9a-f-]{36})", detail)
            title = re.search(r"<title>(.*?)(?: — Reach Campaign)?</title>", detail)
            cache[href] = dict(cr=cr[1] if cr else None, title=html.unescape(title[1]) if title else t,
                               rules=page_text(detail)[:6000])
        out.append(dict(slug=href, mvr=int(m[1].replace(",", "")), budget=int(m[2].replace(",", "")), **cache[href]))
    return out


def norm(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


def merge(cr, reach):
    by_title = {norm(c["title"]): k for k, c in cr.items()}
    for r in reach:
        info = dict(reach_slug=r["slug"], mvr=r["mvr"], reach_url=f"{REACH}/{r['slug']}")
        r["cr"] = r["cr"] or by_title.get(norm(r["title"]))
        if r["cr"] and r["cr"] in cr:
            cr[r["cr"]].update(info)
        else:
            k = r["cr"] or f"reach:{r['slug']}"
            cr[k] = dict(key=k, source="Reach", title=r["title"], brand="", category="Reach", type="pool" if "pool" in r["title"].lower() else None,
                         cpm=None, pool=None, budget_total=r["budget"], budget_left=None, creators=None, platforms=[],
                         application=False, launched=0, description="", url=f"{CR}/c/campaigns/{r['cr']}" if r["cr"] else info["reach_url"], **info)
    return cr


def joined_ids():
    ids = set()
    for s in CFG["streamers"].values():
        for k in ("campaign_url", "alt_campaign_url"):
            m = re.search(r"[0-9a-f-]{36}", s.get(k) or "")
            if m:
                ids.add(m[0])
    return ids


def claude(prompt, system):
    cmd = ["claude", "-p", prompt, "--model", "sonnet", "--strict-mcp-config", "--tools", "", "--system-prompt", system,
           "--disable-slash-commands", "--no-chrome", "--no-session-persistence", "--output-format", "json"]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=tempfile.gettempdir(), timeout=600)
    res = json.loads(r.stdout)
    if res.get("is_error"):
        raise RuntimeError(str(res.get("result"))[:200])
    return res["result"]


def classify(cands):
    rows = [{k: c.get(k) for k in ("key", "title", "brand", "category", "type", "cpm", "budget_total", "platforms", "description")} for c in cands]
    prompt = f"""We run an automated stream-clipping operation: recorders capture live streamers (Twitch, Kick, YouTube Live), cut 15-60s vertical clips, and post them to TikTok, Shorts, Reels and X for paid clipping campaigns. English-language accounts, Australia-based.

Decide for each campaign below whether it fits one of two lanes:
- "gta": pays for clips of GTA VI (GTA 6) streams or videos by a streamer or creator, any size. Song/music edits, product promos and gameplay with no streamer in it don't count.
- "bigname": pays for clips of a big-name streamer, a household name in streaming with a mainstream audience (roughly 1M+ followers on their main platform). Any content counts: IRL, just chatting, any game, brand tie-ins. Examples of the tier: Kai Cenat, IShowSpeed, Marlon, Adin Ross, N3on, xQc, Lacy, JasonTheWeen, Duke Dennis, Fanum, Clix, Jynxzi, Sketch, Plaqueboymax, Agent 00, Kick IRL stars of that size. Smaller or niche creators are NOT a fit unless the campaign is GTA. If you don't recognise the name as that tier, it's not a fit.
Never a fit: non-English campaigns, podcasts and interview shows, movies/TV, finance/business personal brands, faceless content.

CAMPAIGNS:
{json.dumps(rows, ensure_ascii=False, separators=(',', ':'))}

Return ONLY JSON: {{"<key>": {{"fit": true, "lane": "gta|bigname|", "streamer": "name or empty", "why": "one line, max 15 words: what to clip and the catch"}}}} with every key."""
    txt = claude(prompt, "You output only JSON.")
    return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])


def cr_detail(c):
    if c["source"] != "Content Rewards" and not c["url"].startswith(f"{CR}/c/campaigns/"):
        return {}
    s = get(c["url"]).replace('\\"', '"')
    rates = [dict(platform=p, rate=int(r) / 100, min=int(mn) / 100, max=int(mx) / 100) for mx, mn, p, r in
             re.findall(r'\{"maxPayout":(\d+),"minPayout":(\d+),"platform":"(\w+)","rate":(\d+)\}', s)]
    desc = c["description"]
    for m in re.finditer(r'"description":"((?:[^"\\]|\\.)*)"', s):
        try:
            d = json.loads('"' + m[1].replace("\\\\", "\\") + '"')
        except ValueError:
            continue
        if desc[:25] and desc[:25] in d and len(d) > len(desc):
            desc = d
    return dict(rates={r["platform"]: r for r in rates}, full_description=desc[:4000])


def brief(c, reach_rules, detail):
    src = dict(card={k: c.get(k) for k in ("title", "brand", "category", "type", "cpm", "budget_total", "budget_left", "creators", "platforms", "application", "mvr", "url", "reach_url")},
               rates=detail.get("rates"), description=detail.get("full_description") or c["description"], reach_rules_page=reach_rules)
    today = datetime.now().strftime("%-d %b %Y")
    prompt = f"""Write a rules note for a stream clipper deciding whether to join this clipping campaign. Use ONLY the source below. Anything not in it: write "not public until you join". Never invent rates, rules or numbers.

SOURCE:
{json.dumps(src, ensure_ascii=False)[:12000]}

Markdown, exactly these parts:
# <streamer or brand> (<one-line campaign label>)
Back to [[Clip Farming/Clip Farming|Clip Farming]]. Scouted {today} by the campaign scout, **not joined**. Source: <the URLs>.

## Money
bullets: rate per 1k per platform with min/max per clip (rates are USD), or pool size + total view floor (MVR) + how the split works if stated; budget left; clippers already in; needs application or not.
## Footage
bullets: what can be clipped, from where, date limits, who must be primary focus.
## Must have on every clip
"- [ ]" checklist: caption text, hashtags, on-screen text, disclosure.
## Banned
bullets.
## Account + post
bullets: audience regions (say whether Australia counts), niche, submit window, platforms.
## Fit for us
2-3 bullets: is the streamer live often enough for the recorders, does any rule clash with our pipeline (AI-written hooks/captions on real footage, burned-in Kick/Twitch banners, auto-posting via Blotato), worth joining or not.

Style: short plain bullets, no em dashes, no emojis."""
    return claude(prompt, "You write terse markdown notes. Output only the note.").strip()


def notify(text):
    __import__("cfg").notify("Clip scout", text.splitlines()[0][:120].replace('"', "'"))


def pay(c):
    if c.get("cpm"):
        return f"${c['cpm']:g}/1k"
    if c.get("type") == "pool" or c.get("mvr"):
        return f"${(c.get('budget_total') or 0) / 1000:g}k pool" + (f", {c['mvr']:,} view floor" if c.get("mvr") else "")
    return "?"


def safe_name(s):
    return re.sub(r"[^\w .,&()-]+", "", s).strip()[:80] or "campaign"


def main():
    dry = "--dry" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    st = json.loads(SEEN.read_text()) if SEEN.exists() else {"campaigns": {}, "reach": {}}
    seen, baseline = st["campaigns"], not st["campaigns"]
    live = merge(cr_campaigns(), reach_campaigns(st["reach"]))
    now = time.time()
    print(f"{len(live)} live campaigns")

    new = [c for k, c in live.items() if k not in seen]
    recheck = not baseline and st.get("criteria") != CRITERIA
    pool = list(live.values()) if recheck else new
    cands = [c for c in pool if c.get("reach_slug") or ((c["category"] in CATS or KW.search(c["title"] + " " + c["description"]))
                                                       and (c["budget_left"] or 0) >= 100)]
    verdict = {}
    for i in range(0, len(cands), 60):
        verdict.update(classify(cands[i:i + 60]))
    ids = {c["key"] for c in cands}
    for c in pool:
        v, prev = verdict.get(c["key"], {}), seen.get(c["key"], {})
        seen[c["key"]] = dict(first_seen=prev.get("first_seen", now), fit=bool(v.get("fit")), lane=v.get("lane", ""),
                              streamer=v.get("streamer", ""), why=v.get("why", "" if c["key"] in ids else "prefilter: off-category"),
                              title=c["title"], brief=prev.get("brief"))
        if prev.get("brief") and not seen[c["key"]]["fit"] and Path(prev["brief"]).exists():
            (VAULT / "_dropped").mkdir(parents=True, exist_ok=True)
            Path(prev["brief"]).rename(VAULT / "_dropped" / Path(prev["brief"]).name)
            seen[c["key"]]["brief"] = None
    st["criteria"] = CRITERIA
    print(f"{len(new)} new, {'recheck all, ' if recheck else ''}{len(cands)} classified, {sum(1 for c in pool if seen[c['key']]['fit'])} fit")

    joined = joined_ids()
    todo = [k for k, s in seen.items() if s["fit"] and not s.get("brief") and k in live and k not in joined
            and ((live[k]["cpm"] or 0) >= 1 or live[k]["type"] == "pool" or live[k].get("mvr"))]
    if not dry:
        VAULT.mkdir(parents=True, exist_ok=True)
        todo.sort(key=lambda k: (-seen[k]["first_seen"], -(live[k]["cpm"] or 0), -(live[k]["budget_left"] or live[k]["budget_total"] or 0)))
        for k in todo[:BRIEFS_PER_RUN]:
            c = live[k]
            try:
                rules = st["reach"].get(c.get("reach_slug"), {}).get("rules", "")
                md = brief(c, rules, cr_detail(c))
            except (RuntimeError, requests.RequestException, ValueError) as e:
                print(f"brief failed {c['title']}: {e}")
                continue
            p = VAULT / f"{safe_name(seen[k]['streamer'] or c['title'])} - {safe_name(c['title'])}.md"
            p.write_text(md + "\n")
            seen[k]["brief"] = str(p)

    rows = []
    for k, c in live.items():
        s = seen.get(k, {})
        if not s.get("fit"):
            continue
        rows.append(dict(c, description=c["description"][:300], streamer=s.get("streamer", ""), why=s.get("why", ""), lane=s.get("lane", ""),
                         first_seen=s["first_seen"], new=not baseline and now - s["first_seen"] < 86400,
                         joined=k in joined, brief=s.get("brief"), pay=pay(c)))
    rows.sort(key=lambda r: (not r["new"], r["joined"], -(r["cpm"] or 0), -(r["budget_left"] or r["budget_total"] or 0)))
    (OUT / "latest.json").write_text(json.dumps(dict(at=now, live=len(live), rows=rows), indent=1))
    SEEN.write_text(json.dumps(st, indent=1))

    fresh = [r for r in rows if r["first_seen"] == now and not r["joined"]]
    if dry or not fresh:
        return
    head = (f"Clip scout baseline: {len(fresh)} streamer campaigns live. Top 6:" if baseline
            else f"Clip scout: {len(fresh)} new streamer campaign{'s' if len(fresh) > 1 else ''}")
    lines = [head]
    for r in fresh[:6 if baseline else 10]:
        left = f", ${r['budget_left']:,} left" if r.get("budget_left") else ""
        lines.append(f"- {r['title']} ({r['pay']}{left}, {r['creators'] or '?'} clippers). {r['why']}\n  {r.get('reach_url') or r['url']}")
    lines.append("Briefs in vault Clip Farming/Scouted. Desk: http://127.0.0.1:7779 > Campaigns")
    notify("\n".join(lines))


if __name__ == "__main__":
    main()
