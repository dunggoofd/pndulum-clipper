"""Find highlight moments in recorded GTA sessions and cut them into 9:16 clips.

python3 highlight.py            process all sessions touched in the last 40 min (launchd, every 5 min)
python3 highlight.py <dir>      process one session dir
python3 highlight.py --index    rebuild the review page only
python3 highlight.py --prune-dry   list raw sessions older than raw_keep_hours, delete nothing
"""
import fcntl, html, json, os, re, shutil, subprocess, sys, tempfile, time
from datetime import datetime
from pathlib import Path

import cv2, numpy as np, requests, yaml

from views import banner_brand, safe_tags, safe_text

HERE = Path(__file__).parent
CFG = __import__("cfg").load()
ROOT = Path(CFG["root"])
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/130"}
HYPE = re.compile(r"KEKW|OMEGALUL|LUL|LMAO+|LOL|ICANT|PogU|Pog|W{2,}|\?{3,}|CLIP|💀|😂|HOLY|NO WAY|WTF|monkaS|Aware|Clueless|KEKLEO", re.I)
LORE = re.compile(r"\b(" + "|".join(CFG.get("easter_eggs", [])) + r")\b", re.I)
THRESH = 2.6
LEAD, TAIL = 40, 12
JUDGE_MODEL = "sonnet"
PREJUDGE_MODEL = "haiku"   # chat-only first pass before whisper + judge (fix 6, 21 Nov); skips only at >= PREJUDGE_MIN_CONF
PREJUDGE_MIN_CONF = 0.8
SNUB_BOOST = 1   # snub-to-revenge clips (Virality KB, 22 Nov): +1 judge score, capped at 10
MIN_FREE_GB = 10   # below this the HyperFrames overlay render (Chrome frame PNGs in TMPDIR) fails with ENOSPC
YUNET = HERE / "models" / "face_detection_yunet_2023mar.onnx"
BOTS = {"nightbot", "streamelements", "fossabot", "moobot", "botrix", "kickbot", "streamlabs", "sery_bot", "wizebot", "soundalerts"}
BLOCK = re.compile(r"n[i1!|]gg|f[a@4]gg|ret[a@4]rd|\bk[i1]ke|tr[a@4]nn(y|ie)|\bchinks?\b|\bspics?\b|https?://|www\.|\.(com|gg|tv)\b", re.I)


def clean_chat(msgs):
    out = []
    for m in msgs:
        txt = re.sub(r"\[emote:\d+:([^\]]+)\]", r"\1", m["m"]).strip()
        if not txt or txt.startswith("!") or m["u"].lower() in BOTS or BLOCK.search(txt):
            continue
        txt = safe_text(txt)
        if not txt:
            continue
        out.append({"t": m["t"], "u": m["u"], "m": txt if len(txt) <= 70 else txt[:68].rstrip() + "…"})
    return out


def chat_overlay(msgs, t0, ci, co):
    """Messages for the replay box, clip-relative. t0 = wall clock of clip start, ci/co = box in/out."""
    rel = [{**m, "t": round(m["t"] - t0, 2)} for m in clean_chat(sorted(msgs, key=lambda m: m["t"]))]
    pre = [{**m, "t": ci} for m in rel if ci - 10 <= m["t"] < ci][-6:]
    live, last = [], -9
    for m in rel:
        if ci <= m["t"] <= co and m["t"] - last >= 0.35:
            live.append(m)
            last = m["t"]
    return {"in": round(ci, 2), "out": round(co, 2), "msgs": pre + live} if len(pre + live) >= 4 else None


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def robust_z(x, cov, win=600, floor=1e-6):
    z = np.zeros_like(x)
    for i in range(len(x)):
        lo = max(0, i - win)
        w = x[lo:i + 1][cov[lo:i + 1]]
        if len(w) < 60:
            continue
        med = np.median(w)
        mad = max(np.median(np.abs(w - med)) * 1.4826, floor)
        z[i] = (x[i] - med) / mad
    return np.clip(z, -5, 10)


def smooth(x, k=5):
    return np.convolve(x, np.ones(k) / k, mode="same")


def segments(d):
    return sorted((int(p.stem), p) for p in d.glob("*.ts") if p.stem.isdigit())


def seg_rms(p):
    cache = p.with_suffix(".rms.json")
    if cache.exists():
        return json.loads(cache.read_text())
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(p), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    n = len(a) // 8000
    rms = [float(np.sqrt(np.mean(a[i * 8000:(i + 1) * 8000] ** 2)) + 1) for i in range(n)]
    cache.write_text(json.dumps(rms))
    return rms


def seg_motion(p, cam):
    cache = p.with_suffix(".mot.json")
    if cache.exists():
        return json.loads(cache.read_text())
    crop = f"crop=iw*{cam[2]}:ih*{cam[3]}:iw*{cam[0]}:ih*{cam[1]},scale=64:48,format=gray,showinfo"
    r = subprocess.run(["ffmpeg", "-loglevel", "info", "-skip_frame", "nokey", "-i", str(p), "-an", "-vf", crop,
                        "-fps_mode", "passthrough", "-f", "rawvideo", "-"], capture_output=True)
    ts = [float(t) for t in re.findall(rb"pts_time:([0-9.]+)", r.stderr)]
    f = np.frombuffer(r.stdout, dtype=np.uint8).reshape(-1, 48, 64).astype(np.int16)[:len(ts)]
    tex = np.abs(np.diff(f, axis=1)).mean(axis=(1, 2)) + np.abs(np.diff(f, axis=2)).mean(axis=(1, 2))
    f = f - f.mean(axis=(1, 2), keepdims=True).astype(np.int16)
    mot = [0.0] * 62
    for k in range(1, len(f)):
        ch = float((np.abs(f[k] - f[k - 1]) > 20).mean()) if min(tex[k], tex[k - 1]) >= 10 else 0.0
        for s in range(int(ts[k - 1]), min(int(ts[k]) + 1, 62)):
            mot[s] = ch if ch < 0.9 else 0.0
    cache.write_text(json.dumps(mot))
    return mot


def cam_motion(segs, cam, t0, n):
    mot = np.zeros(n)
    if not cam:
        return mot
    for st, p in segs[:-1]:
        for i, v in enumerate(seg_motion(p, cam)):
            if 0 <= st - t0 + i < n:
                mot[st - t0 + i] = v
    return mot


def fan_clips(meta):
    try:
        if meta["platform"] == "youtube":
            return []
        if meta["platform"] == "kick":
            c = requests.get(f"https://kick.com/api/v2/channels/{meta['name']}/clips?sort=date&time=day", headers=UA, timeout=15).json()["clips"]
            return [datetime.fromisoformat(x["created_at"].replace("Z", "+00:00")).timestamp() for x in c]
        q = {"query": '{user(login:"%s"){clips(first:100,criteria:{period:LAST_DAY}){edges{node{createdAt}}}}}' % CFG["streamers"][meta["name"]].get("login", meta["name"])}
        r = requests.post("https://gql.twitch.tv/gql", json=q, headers={"Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko"}, timeout=15).json()
        return [datetime.fromisoformat(e["node"]["createdAt"].replace("Z", "+00:00")).timestamp() for e in r["data"]["user"]["clips"]["edges"]]
    except Exception as e:
        log("fan clips err", e)
        return []


def signals(d, meta, segs):
    t0 = segs[0][0]
    t1 = segs[-1][0]
    n = t1 - t0
    if n < 180:
        return None
    rms = np.ones(n)
    cov = np.zeros(n, dtype=bool)
    for st, p in segs[:-1]:
        for i, v in enumerate(seg_rms(p)):
            if 0 <= st - t0 + i < n:
                rms[st - t0 + i] = v
                cov[st - t0 + i] = True
    for i in np.flatnonzero(~cov):
        if i > 0 and cov[max(0, i - 5):i].any() and cov[i + 1:i + 6].any():
            rms[i], cov[i] = rms[i - 1], True
    chat = np.zeros(n)
    hype = np.zeros(n)
    lore = np.zeros(n)
    seen = np.zeros(n)
    lore_by = {}
    kick_rate = []
    msgs = []
    for ln in open(d / "chat.jsonl"):
        try:
            m = json.loads(ln)
        except json.JSONDecodeError:
            continue
        i = int(m["t"] - t0)
        if not 0 <= i < n:
            continue
        if "rate" in m:
            kick_rate.append((i, m["rate"]))
            continue
        msgs.append(m)
        seen[i] += 1
        if LORE.search(m["m"]) and i - lore_by.get(m["u"], -99) >= 60:
            lore[i] += 1
            lore_by[m["u"]] = i
        if meta["platform"] in ("twitch", "youtube"):
            chat[i] += 1
        if HYPE.search(m["m"]):
            hype[i] += 1
    if kick_rate:
        xs, ys = zip(*kick_rate)
        chat = np.interp(np.arange(n), xs, ys)
    clipsig = np.zeros(n)
    for ct in fan_clips(meta):
        i = int(ct - t0)
        if 0 <= i < n:
            clipsig[max(0, i - 20):i + 5] += 1
    db = 20 * np.log10(rms)
    lw, sw = np.convolve(lore, np.ones(15), mode="same"), np.convolve(seen, np.ones(15), mode="same")
    loresig = np.where(lw >= 3, np.minimum(lw / np.maximum(sw, 1) / 0.05, 3), 0)
    mot = cam_motion(segs, CFG["streamers"][meta["name"]].get("cam"), t0, n)
    motsig = np.clip(robust_z(smooth(mot, 3), cov, floor=0.02), 0, 4)
    score = robust_z(smooth(chat, 5), cov) + 0.6 * robust_z(smooth(hype, 5), cov) + 0.5 * robust_z(smooth(db, 3), cov) + 0.8 * np.minimum(clipsig, 3) + 0.8 * loresig + 0.5 * motsig
    ok = np.array([cov[max(0, i - 240):i + 45].all() and 240 <= i <= n - 45 for i in range(n)])
    score[~ok] = 0
    return t0, score, msgs


def peaks(t0, score, done):
    out = []
    order = np.argsort(score)[::-1]
    taken = [p - t0 for p in done]
    for i in order:
        if score[i] < THRESH:
            break
        if any(abs(i - j) < 120 for j in taken):
            continue
        taken.append(i)
        out.append((t0 + int(i), float(score[i])))
    return out


def extract(segs, start, end, out, video=True):
    parts = [p for st, p in segs if st + 62 > start and st < end]
    first = min(st for st, p in segs if st + 62 > start)
    lst = Path(tempfile.mktemp(suffix=".txt"))
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-ss", str(max(0, start - first)), "-t", str(end - start)]
    cmd += ["-c", "copy", str(out)] if video else ["-vn", "-ac", "1", "-ar", "16000", str(out)]
    subprocess.run(cmd, check=True)
    lst.unlink()


_FW = None


def transcribe(wav):
    global _FW
    try:
        import mlx_whisper
    except ImportError:
        from faster_whisper import WhisperModel
        _FW = _FW or WhisperModel("large-v3-turbo", device="auto", compute_type="auto")
        segs, _ = _FW.transcribe(str(wav), word_timestamps=True, language="en")
        return [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)} for s in segs for w in (s.words or [])]
    r = mlx_whisper.transcribe(str(wav), path_or_hf_repo="mlx-community/whisper-large-v3-turbo", word_timestamps=True, language="en")
    return [{"w": w["word"].strip(), "s": round(w["start"], 2), "e": round(w["end"], 2)} for s in r["segments"] for w in s.get("words", [])]


def prejudge(meta, chat_sample, peak_rel, dur, body=""):
    """Cheap first look (Haiku, no transcript): kill the obvious losers before whisper + the Sonnet judge.
    Only skips on clear evidence. Anything uncertain goes through. Returns (skip, reason)."""
    brief = CFG["streamers"][meta["name"]].get("brief")
    prompt = f"""A chat-velocity detector flagged t={peak_rel:.0f}s of a {dur:.0f}s live-stream window. Streamer: {meta['name']}, {brief or 'playing GTA VI'}. Stream title: {meta['title']}
You see only chat and audio/webcam evidence, no transcript. Decide whether this moment is CERTAINLY not clip material.

CHAT (seconds from window start, sampled):
{chat_sample}
{body}
Skip ONLY when the chat makes one of these obvious: a raid, sub bomb or gifted-subs train, giveaway or poll, stream lag, freeze or crash ("lag", "frozen", "stream died", "F in chat" for the stream), an ad, sponsor read or intermission, BRB / AFK / bathroom break, the streamer asleep or away, chat reacting to something off-stream (a different streamer, a tweet) with nothing happening here, or pure emote spam with no event behind it.
Everything else goes through, including quiet chat, confusing chat and reactions you can't explain. A big reaction to an unknown event is NOT a skip.
Reply with ONLY JSON: {{"skip": true/false, "confidence": 0-1, "reason": "one line"}}"""
    cmd = ["claude", "-p", prompt, "--model", PREJUDGE_MODEL, "--strict-mcp-config", "--tools", "", "--system-prompt", "You output only JSON.",
           "--disable-slash-commands", "--no-chrome", "--no-session-persistence", "--output-format", "json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=tempfile.gettempdir(), timeout=60)
        res = json.loads(r.stdout)
        txt = res.get("result") or ""
        j = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except Exception as e:
        log("prejudge err", str(e)[:120])
        return False, ""
    return bool(j.get("skip")) and float(j.get("confidence") or 0) >= PREJUDGE_MIN_CONF, str(j.get("reason") or "")[:160]


def snub_variant():
    """A/B for snub clips that have a payoff in the window: alternate ending on the intent line vs on the payoff."""
    f = ROOT / "snub_ab.json"
    n = json.loads(f.read_text()).get("n", 0) if f.exists() else 0
    f.write_text(json.dumps({"n": n + 1}))
    return "payoff" if n % 2 else "intent"


def judge(meta, words, chat_sample, peak_rel, dur, body=""):
    lines, cur, cs = [], [], None
    for w in words:
        if cs is None:
            cs = w["s"]
        cur.append(w["w"])
        if len(cur) >= 12:
            lines.append(f"[{cs:5.1f}] {' '.join(cur)}")
            cur, cs = [], None
    if cur:
        lines.append(f"[{cs:5.1f}] {' '.join(cur)}")
    brief = CFG["streamers"][meta["name"]].get("brief")
    prompt = f"""You pick viral short-form clips from live streams. Streamer: {meta['name']} ({meta['platform']}), {brief or 'playing GTA VI'}. Stream title: {meta['title']}
Below is a {dur:.0f}s window. Chat exploded around t={peak_rel:.0f}s (chat reacts 3-15s after the event).

TRANSCRIPT (seconds from window start):
{chr(10).join(lines) or '(no speech)'}

CHAT (seconds from window start, sampled):
{chat_sample}
{body}
Hard rejects: isolated cutscenes or story spoilers with no streamer reaction, the game's ending or a major story reveal (Rockstar takes these down), a transcript that is mostly song lyrics from the in-game radio (Content ID claim), ads/intermissions, the streamer not being the focus.
Decide if this is a clip worth posting to TikTok/Shorts/Reels. Good clips: a clear setup and payoff, a big reaction, a funny line, a fail, a rage, a wild moment, drama a viewer gets with zero context. Bad: ads, dead air, reading chat with no payoff, unclear context.
OLD-GTA EASTER EGGS score high even with a smaller reaction: anything in GTA VI that calls back to an older GTA (a returning character, name-drop, place, brand, radio host, item, myth). Known so far: "Lethal by Design: A Liberty City Memoir" book in Jason's house (GTA IV), Eddie Low podcast episode (GTA IV), Wade Hebert true-crime podcast (GTA V), Fabien LaRouche magazine (GTA V), boat GPS showing Los Santos (GTA V), Skunk Ape / Bigfoot side quest (San Andreas myth), UFO abduction after 2am, Mount Chiliad talk, Tommy Vercetti shirt / Vercetti Estate / Ocean View Hotel / V-Rock (Vice City), Phil Cassidy and Ammu-Nation, Big Smoke Cigar Co. and Grove Street / CJ (San Andreas), Gay Tony, Jimmy / Michael De Santa, Trevor, Franklin, Lamar, Niko Bellic and cousin Roman. Keep it when the reference is clear on screen or the streamer or chat clearly clocks it ("yo that's from San Andreas", chat spamming the name). Open the clip on the reference itself, not the walk up to it.
Easter-egg hooks use the "who's back" reveal shape, never naming the character in the hook so the payoff lands in the clip: "YOU WOULDN'T BELIEVE WHO RETURNS IN GTA 6 😳", "GTA 6 BROUGHT BACK A GTA 4 LEGEND 😭", "OG GTA FANS WILL GET THIS 💀", "ROCKSTAR HID THIS FOR THE OG FANS 👀", "NOBODY NOTICED THIS GTA 5 CALLBACK". Keep it true: "returns/back" only when the character, place or thing is actually shown or named in GTA VI; for a name-drop or item say "MENTIONED", "HID", "REFERENCE". Caption names the old game and asks a real question (e.g. "Who else from the old games do you want back?"). Add the old game's tag (gta4, gtasanandreas, gtavicecity, gta5) to hashtags.
ACT-IT-OUT moments score high: something in the game provokes the streamer the way people get provoked in real life (cut off in traffic, yelled at from a car, an NPC or player disrespects him, road rage), he gets up out of his seat and acts out on camera what his character does (squares up, carries someone, throws, fights), escalating to one line he repeats ("go for a swim", "goodnight"). Every viewer has felt the provocation; the game is the revenge. The provocation has to be in the clip. WEBCAM MOVEMENT above is the evidence he left his seat. Open seated just before the provocation so he stands up inside the first 5s, end on the payoff or his repeated line. Best hook is a POV that makes the viewer the wronged one ("POV: SOMEONE CUTS YOU OFF 😤", "POV: THEY HONK AT YOU 💀"). Set "act_out": true for these.
SNUB-TO-REVENGE moments score high even with no movement and no yelling (the streamer can stay seated and talk calmly): the transcript runs 1) a friendly move that gets snubbed (he says hi or good morning, compliments, offers help or a ride, and an NPC or player ignores him, stares, walks off or is rude), 2) his indignant rant about it ("this is what's wrong with today's society", "I said good morning bro"), 3) a surprise he finds or that happens (something random in the world: an animal, an item, an unalived critter on the road, a car, a stranger), 4) one line where he decides to use it on the person who snubbed him ("I'm gonna throw it at him"). Every viewer has been blanked by a stranger; the surprise is the twist nobody predicts. Needs at least the snub, the rant and the intent line in the window. Open on the greeting itself, never the walk up. "out" ends right after the intent line, BEFORE the revenge plays out. "payoff_out": the second where the revenge and his reaction to it finish, if that happens inside the window, else null. Hooks for these use the two-beat story shape, setup then a cut-off twist, never saying what the twist is: "HE SAID GOOD MORNING... THEN THIS 💀", "HE JUST WANTED TO SAY HI 😭", "HE GOT IGNORED SO HE DID THIS 😳". Keep the animal or object out of the hook. Caption asks a real question about the twist. Set "snub": true for these.
PUNCH-IN beats ("focus"): the edit cuts from the cam/gameplay split to the streamer's cam full screen while he PERFORMS for the camera, then cuts back. Performing = singing or belting along (the held note), screaming or a hype yell, a celebration ("WE DID IT"), a bit or impression aimed at the camera, dancing, acting it out, a dramatic reaction face, crying-laughing, a rant line delivered to chat. Not performing = explaining, narrating gameplay, reading, calm talk, or when the payoff is happening in the game (the viewer must see the game then). Each beat: start on the first word of the performed line (or 0.2s before the yell), end when the line or yell ends, 1.2-4s long, at least 2s between beats, none before 1.5s (the hook needs to be read), total under 40% of the clip. LOUD and WEBCAM MOVEMENT above are the evidence. Usually 1-3 beats; [] if he never performs.
Pick in/out points inside the window: 15-60s long, open on the setup (first 2s must hook), end right after the payoff or reaction, never mid-sentence.
Reply with ONLY JSON:
{{"keep": true/false, "score": 1-10, "in": seconds, "out": seconds, "hooks": ["3 different on-screen hooks, each max 8 words, ALL CAPS, curiosity not spoiler, 1-2 emojis that fit the moment (e.g. 😭💀🔥😳🚔), best first"], "caption": "post caption, under 120 chars, no hashtags, makes people comment", "hashtags": ["4-6 hashtags without #, niche first"], "chat": null or {{"in": seconds, "out": seconds}}, "rage": true/false, "easter_egg": null or "short name of the old-GTA reference and its game", "act_out": true/false, "snub": true/false, "payoff_out": null or seconds, "focus": [{{"in": seconds, "out": seconds}}], "reason": "one line"}}
Hooks, caption and hashtags get read by TikTok/IG/YouTube moderation (on-screen text is OCR'd). Never use: kill/murder/die/dead/death/suicide, shoot/shot/gun or weapon names, blood, stab, bomb, kidnap, sex/nude/stripper/hooker, drug names, slurs, swearing (fuck/shit/bitch). The word "kill" in any form (kills, killed, killing, roadkill, killstreak, killer) never appears: always write "unalived" / "unalive" instead. Other safe swaps that keep the energy: cooked, took out, blasted, pew pew, spicy, contraband. No 🔫💊💉🍆🍑💦🩸🔪💣🚬. Hook voice: talk like Gen Z clip pages. Use slang where it lands naturally, max one per hook, never forced: aura (+1000 aura for a clutch move, -aura for an L), ragebait, chopped (ugly or ruined), cooked (doomed), fine shyt (an attractive character), sybau (shut up), rizz (flirting skill), gang (addressing viewers), downbad (desperate), motion (on a run, winning), glazing (overpraising). Spell them exactly like that.
No engagement bait in the caption ("comment below", "tag a friend", "like and follow"); ask a real question instead.
"chat": set it in two cases. 1) The streamer talks to chat inside the clip (reads a message out, answers chat, argues with or reacts to what chat says, chat dares him): the window seconds where that happens, start a few seconds before he engages. 2) The streamer rages (yelling, screaming at the game or someone, slamming the desk, swearing meltdown, "I'm so done"): the window from the rage start to 8s after, so viewers see chat laughing at it. null otherwise, and null when he just says "chat" as a filler word.
"rage": true if the clip's main moment is the streamer raging, else false."""
    cmd = ["claude", "-p", prompt, "--model", JUDGE_MODEL, "--strict-mcp-config", "--tools", "", "--system-prompt", "You output only JSON.",
           "--disable-slash-commands", "--no-chrome", "--no-session-persistence", "--output-format", "json"]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=tempfile.gettempdir(), timeout=180)
    res = json.loads(r.stdout)
    if res.get("is_error") or "limit" in str(res.get("result", ""))[:80].lower():
        raise RuntimeError("judge failed: " + str(res.get("result"))[:200])
    txt = res["result"]
    return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])


def cam_region(src, cam):
    cap = cv2.VideoCapture(str(src))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 300
    W, H = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    hits, boxes = 0, []
    for k in range(8):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * (k + 0.5) / 8))
        ok, f = cap.read()
        if not ok:
            continue
        if cam:
            x, y, w, h = int(cam[0] * W), int(cam[1] * H), int(cam[2] * W), int(cam[3] * H)
            roi = cv2.resize(f[y:y + h, x:x + w], (w * 3, h * 3))
            det = cv2.FaceDetectorYN_create(str(YUNET), "", (w * 3, h * 3), 0.7)
            _, fs = det.detect(roi)
            hits += fs is not None
        else:
            det = cv2.FaceDetectorYN_create(str(YUNET), "", (int(W), int(H)), 0.8)
            _, fs = det.detect(f)
            if fs is not None:
                b = max((b[:4] for b in fs), key=lambda b: b[2] * b[3])
                if b[3] > H * 0.06:
                    boxes.append(b)
    cap.release()
    if cam:
        return ((cam[0] * W, cam[1] * H, cam[2] * W, cam[3] * H) if hits >= 4 else None), W, H
    if len(boxes) < 4 or np.std(np.array(boxes)[:, :2], axis=0).max() > H * 0.08:
        return None, W, H
    x, y, w, h = np.median(np.array(boxes), axis=0)
    cw = min(W, w * 3.2)
    ch = cw * 640 / 1080
    return (min(max(x + w / 2 - cw / 2, 0), W - cw), min(max(y + h / 2 - ch / 2, 0), H - ch), cw, ch), W, H


def vertical(src, overlay, out, cam=None, trim=0.0, focus=None):
    box, W, H = cam_region(src, cam)
    H = int(H * (1 - trim)) // 2 * 2
    focus = focus if box is not None else None
    pre = f"[0:v]crop=iw:{H}:0:0,split={4 if focus else 2}[s0][s1]{'[s2][s3]' if focus else ''};"
    if box is not None:
        x, y, w, h = box
        side = min(w, h)
        fx, fy = x + (w - side) / 2, min(y + (h - side) / 2, H - side)
        if w / h > 1080 / 640:
            nw = h * 1080 / 640
            x, w = x + (w - nw) / 2, nw
        else:
            nh = w * 640 / 1080
            y, h = y + (h - nh) / 2, nh
        y = min(y, H - h)
        gw = H * 1080 / 1280
        fc = pre + (f"[s0]crop={w:.0f}:{h:.0f}:{x:.0f}:{y:.0f},scale=1080:640:flags=lanczos,setsar=1[top];"
              f"[s1]crop={gw:.0f}:{H:.0f}:(iw-{gw:.0f})/2:0,scale=1080:1280:flags=lanczos,setsar=1[bot];"
              f"[top][bot]vstack[split]")
        if focus:
            on = "+".join(f"between(t,{f['in']:.2f},{f['out']:.2f})" for f in focus)
            fc += (f";[s2]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,scale=1300:-2,crop=1080:1920,boxblur=30:2,eq=brightness=-0.12[hbg];"
                   f"[s3]crop={side:.0f}:{side:.0f}:{fx:.0f}:{fy:.0f},scale=1080:1080:flags=lanczos,unsharp=5:5:0.6,setsar=1[hfg];"
                   f"[hbg][hfg]overlay=0:150[hero];[split][hero]overlay=0:0:enable='{on}'[base]")
        else:
            fc += ";[split]null[base]"
    else:
        fc = pre + ("[s0]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:2,eq=brightness=-0.08[bg];"
              "[s1]crop=iw/1.25:ih,scale=1080:-2,setsar=1[fg];[bg][fg]overlay=0:(H-h)/2[base]")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
    if overlay:
        cmd += ["-c:v", "libvpx-vp9", "-i", str(overlay)]
        fc += ";[base][1:v]overlay=0:0:format=auto[v]"
    else:
        fc += ";[base]null[v]"
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "0:a", "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-af", "loudnorm=I=-14:TP=-1.5", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)
    return "facecam-split" if box is not None else "blur-fill"


def snap(words, a, b, dur):
    ends = lambda i: words[i]["w"][-1:] in ".!?" or i + 1 == len(words) or words[i + 1]["s"] - words[i]["e"] >= 0.6
    talking = lambda t: any(w["s"] - 0.3 <= t <= w["e"] + 0.3 for w in words)
    if talking(a):
        i = next((k for k, w in enumerate(words) if w["e"] > a), None)
        while i is not None and i > 0 and not ends(i - 1) and a - words[i - 1]["s"] <= 5:
            i -= 1
        if i is not None and (i == 0 or ends(i - 1)):
            a = max(0.0, min(a, words[i]["s"] - 0.15))
    if talking(b):
        pad = lambda k: min(words[k]["e"] + 0.3, words[k + 1]["s"] - 0.05 if k + 1 < len(words) else dur)
        fwd = [pad(k) for k, w in enumerate(words) if ends(k) and b - 0.3 <= w["e"] <= min(b + 6, dur - 0.3)]
        back = [pad(k) for k, w in enumerate(words) if ends(k) and a + 12 <= w["e"] < b]
        if fwd:
            b = fwd[0]
        elif back:
            b = back[-1]
    return a, min(b, dur)


def make_clip(d, meta, segs, msgs, peak, score):
    start, end = peak - LEAD, peak + TAIL
    work = d / "work" / str(peak)
    work.mkdir(parents=True, exist_ok=True)
    extract(segs, start, end, work / "window.wav", video=False)
    near, last = [], -9
    for m in clean_chat(sorted((m for m in msgs if start <= m["t"] <= end), key=lambda m: m["t"])):
        dense = peak - 12 <= m["t"] <= peak + 6
        if dense or m["t"] - last >= 1.0:
            near.append(f"[{m['t'] - start:5.1f}] {m['u']}: {m['m']}")
            last = m["t"]
    chat_sample = "\n".join(near[:140]) or "(none captured)"
    mot = cam_motion(segs, CFG["streamers"][meta["name"]].get("cam"), start, end - start)
    moving = np.flatnonzero(mot > max(0.15, 3 * np.median(mot)))
    body = ""
    if len(moving):
        runs = np.split(moving, np.flatnonzero(np.diff(moving) > 2) + 1)
        body = "WEBCAM MOVEMENT (streamer's body moves a lot on cam, e.g. out of his seat or acting it out), seconds: " + ", ".join(
            f"{r[0]}-{r[-1] + 1}" for r in runs) + "\n"
    rms = np.array(seg_rms(work / "window.wav"))
    loud = np.flatnonzero(rms > 2 * np.median(rms))
    if len(loud):
        runs = np.split(loud, np.flatnonzero(np.diff(loud) > 1) + 1)
        body += "LOUD (audio peaks, usually the streamer yelling or singing), seconds: " + ", ".join(f"{r[0]}-{r[-1] + 1}" for r in runs) + "\n"
    skip, why = prejudge(meta, chat_sample, LEAD, end - start, body)
    if skip:
        return {"peak": peak, "signal": round(score, 2), "streamer": meta["name"], "title": meta["title"], "status": "prejudged",
                "judge": {"keep": False, "score": 0, "hook": "", "reason": "prejudge: " + why},
                "campaign": CFG["streamers"][meta["name"]].get("campaign", "none (no paid campaign found)")}
    extract(segs, start, end, work / "window.ts")
    words = transcribe(work / "window.wav")
    j = judge(meta, words, chat_sample, LEAD, end - start, body)
    if j.get("keep") and not (j.get("hooks") and j.get("caption")):
        j = judge(meta, words, chat_sample, LEAD, end - start, body)
    j["hooks"] = [h for h in (safe_text(h.strip()) for h in j.get("hooks") or [j.get("hook", "")] if h and h.strip()) if h]
    j["hook"] = j["hooks"][0] if j["hooks"] else ""
    j["caption"] = safe_text(j.get("caption") or "") or ""
    j["hashtags"] = safe_tags(list(dict.fromkeys([t.lstrip("#") for t in j.get("hashtags") or []] + ([] if CFG["streamers"][meta["name"]].get("brief") else ["gta6"]) + [CFG["streamers"][meta["name"]].get("login", meta["name"])])))
    if j.get("keep") and not (j["hook"] and j.get("caption")):
        j.update(keep=False, reason="judge returned no hook or caption")
    tail = CFG["streamers"][meta["name"]].get("caption_tail")
    if tail and j.get("caption"):
        j["caption"] = f"{j['caption']} {tail}"
    rec = {"peak": peak, "signal": round(score, 2), "judge": j, "streamer": meta["name"], "title": meta["title"],
           "campaign": CFG["streamers"][meta["name"]].get("campaign", "none (no paid campaign found)")}
    if j.get("snub") and j.get("keep"):
        j["score"] = min(10, j.get("score", 0) + SNUB_BOOST)
    if not j.get("keep") or j.get("score", 0) < 6:
        rec["status"] = "rejected"
        return rec
    a, b = float(j["in"]), float(j["out"])
    if j.get("snub"):
        po = j.get("payoff_out")
        po = float(po) if isinstance(po, (int, float)) and float(po) > b + 1 else None
        rec["snub_end"] = "intent" if po is None else snub_variant()
        if po is not None and rec["snub_end"] == "payoff":
            b = po
    b = min(max(b, a + 12), a + 60, end - start)
    a, b = snap(words, a, b, end - start)
    cw = [{"w": safe_text(w["w"]) or "", "s": round(w["s"] - a, 2), "e": round(w["e"] - a, 2)} for w in words if w["s"] >= a and w["e"] <= b]
    cw = [w for w in cw if w["w"]]
    focus, used = [], 0.0
    for f in sorted((f for f in j.get("focus") or [] if isinstance(f, dict)), key=lambda f: float(f["in"])):
        fi, fo = max(float(f["in"]) - a, 1.5), min(float(f["out"]) - a, b - a, float(f["in"]) - a + 4)
        if focus and fi - focus[-1]["out"] < 2:
            continue
        if fo - fi >= 1.0 and used + fo - fi <= 0.4 * (b - a):
            focus.append({"in": round(fi, 2), "out": round(fo, 2)})
            used += fo - fi
    day =datetime.fromtimestamp(peak).strftime("%Y%m%d")
    slug = re.sub(r"[^a-z0-9]+", "-", j["hook"].lower()).strip("-")[:40]
    outdir = ROOT / "clips" / day
    outdir.mkdir(parents=True, exist_ok=True)
    name = f"{meta['name']}-{datetime.fromtimestamp(peak).strftime('%H%M%S')}-{slug}"
    extract(segs, start + a, start + b, work / "clip.ts")
    ov = None
    renderer = HERE / "overlay" / "render_overlay.py"
    if renderer.exists() and free_gb() < MIN_FREE_GB:
        log("overlay skipped, only", round(free_gb()), "GB free")
        renderer = HERE / "overlay" / "missing"
    if renderer.exists():
        cj = work / "clip.json"
        split = cam_region(work / "clip.ts", CFG["streamers"][meta["name"]].get("cam"))[0] is not None
        sc = CFG["streamers"][meta["name"]]
        logo = banner_brand(sc)
        kick = sc["url"] if logo else None
        rec["banner"] = logo
        pos = {"hook_y": 730, "kick_y": 600} if split and kick else {"hook_y": 520} if split else {"hook_y": 300, "kick_y": 160} if kick else {}
        pos.update({"chat_y": 1000, "chat_h": 220} if split and kick else {"chat_y": 790, "chat_h": 420} if split else {"chat_y": 620, "chat_h": 440})
        jc = j.get("chat") if isinstance(j.get("chat"), dict) else None
        chat = None
        if jc:
            ci, co = max(0.0, float(jc["in"]) - a), min(b - a, float(jc["out"]) - a)
            chat = chat_overlay(msgs, start + a, ci, co) if co - ci >= 2 else None
        rec["chat"] = {"in": chat["in"], "out": chat["out"], "n": len(chat["msgs"])} if chat else None
        cj.write_text(json.dumps({"duration": round(b - a, 2), "hook": j["hook"], "streamer": meta["name"], "words": cw,
                                  "handle": sc.get("handle", "@" + sc.get("login", meta["name"])),
                                  **({"kick": kick, "logo": f"assets/{logo}-logo.svg"} if kick else {}), **({"chat": chat} if chat else {}),
                                  **({"focus": focus} if split and focus else {}), **pos}))
        r = subprocess.run([sys.executable, str(renderer), str(cj), str(work / "overlay.webm")], capture_output=True, text=True)
        ov = work / "overlay.webm" if r.returncode == 0 else None
        if ov is None:
            log("overlay failed", r.stderr[-300:])
    layout = vertical(work / "clip.ts", ov, outdir / f"{name}.mp4", CFG["streamers"][meta["name"]].get("cam"),
                      CFG["streamers"][meta["name"]].get("trim_bottom", 0.0), focus)
    rec.update(status="cut", file=f"{day}/{name}.mp4", layout=layout, overlay=bool(ov), duration=round(b - a, 1),
               cta=(CFG["streamers"][meta["name"]].get("handle") or "") if ov else "",
               focus=focus if layout == "facecam-split" else [],
               words=" ".join(w["w"] for w in cw))
    (outdir / f"{name}.json").write_text(json.dumps(rec, indent=1))
    return rec


def process(d):
    meta = json.loads((d / "meta.json").read_text())
    segs = segments(d)
    if len(segs) < 4:
        return
    state_f = d / "moments.json"
    state = json.loads(state_f.read_text()) if state_f.exists() else []
    sig = signals(d, meta, segs)
    if not sig:
        return
    t0, score, msgs = sig
    for peak, s in peaks(t0, score, [m["peak"] for m in state])[:1]:
        log(meta["name"], "peak", datetime.fromtimestamp(peak).strftime("%H:%M:%S"), round(s, 2))
        try:
            rec = make_clip(d, meta, segs, msgs, peak, s)
        except Exception as e:
            log("clip failed", e)
            rec = {"peak": peak, "signal": round(s, 2), "status": "error", "error": str(e)[:300]}
            if "judge failed" in str(e):
                break
        state.append(rec)
        state_f.write_text(json.dumps(state, indent=1))
        log(" ->", rec["status"], rec.get("judge", {}).get("hook", ""))
        if rec["status"] == "cut":
            try:
                cv = subprocess.run([sys.executable, str(HERE / "cover.py"), str((ROOT / "clips" / rec["file"]).with_suffix(".json"))],
                                    capture_output=True, text=True, timeout=600)
                log(" -> cover", "ok" if cv.returncode == 0 else "failed " + cv.stderr.strip()[-200:])
            except subprocess.TimeoutExpired:
                log(" -> cover timed out")
            notify(f"{meta['name']}: {rec['judge']['hook']}")


def notify(msg):
    __import__("cfg").notify("Clipper", msg)


def index():
    rows = []
    for f in sorted((ROOT / "clips").glob("[0-9]*/[!.]*.json"), reverse=True):
        r = json.loads(f.read_text())
        j = r.get("judge")
        if not j or "peak" not in r or "file" not in r:
            continue   # hand-made clips (e.g. marlon-format-b) carry no judge record
        rows.append(f"""<article><video src="{html.escape(r['file'])}" controls preload="metadata" playsinline></video>
<div class="m"><div class="k">{html.escape(r['streamer'])} · {html.escape(r.get('campaign',''))} · {datetime.fromtimestamp(r['peak']).strftime('%d %b %H:%M')} · {r['duration']}s · score {j['score']}/10 · signal {r['signal']}</div>
<h2>{html.escape(j['hook'])}</h2><p class="c">{html.escape(j['caption'])}</p><p class="r">{html.escape(j['reason'])}</p>
<button onclick="navigator.clipboard.writeText(this.previousElementSibling.previousElementSibling.textContent)">Copy caption</button></div></article>""")
    page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GTA Clip Queue</title>
<style>:root{{--bg:#0b0b0c;--card:#151517;--ink:#f2f2f2;--mute:#9a9aa0;--acc:#ffe14d}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,Inter,sans-serif;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}}.sub{{color:var(--mute);margin-bottom:20px}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}}
article{{background:var(--card);border-radius:12px;overflow:hidden}}video{{width:100%;aspect-ratio:9/16;background:#000;display:block}}
.m{{padding:12px}}.k{{color:var(--mute);font-size:12px}}h2{{font-size:16px;margin:6px 0;color:var(--acc)}}.r{{color:var(--mute);font-size:13px}}
button{{background:var(--acc);border:0;border-radius:6px;padding:6px 10px;font-weight:600;cursor:pointer}}</style>
<h1>GTA Clip Queue</h1><div class="sub">{len(rows)} clips · rebuilt {datetime.now().strftime('%d %b %H:%M')} · newest first</div><main>{''.join(rows)}</main>"""
    (ROOT / "clips").mkdir(parents=True, exist_ok=True)
    (ROOT / "clips" / "index.html").write_text(page)


def free_gb(path=None):
    st = os.statvfs(path or ROOT)
    return st.f_bavail * st.f_frsize / 1e9


def archive():
    arc = Path(CFG["archive"])
    if not os.path.ismount(CFG["archive_volume"]) or not os.access(CFG["archive_volume"], os.W_OK):
        free = free_gb()
        if free < 25:
            notify(f"Seagate not plugged in or not writable, {free:.0f}GB free on Mac")
        return
    cutoff = time.time() - CFG["local_keep_hours"] * 3600
    for p in list((ROOT / "raw").glob("*/[0-9]*/[0-9]*.ts")):
        try:
            if p.stat().st_mtime >= cutoff:
                continue
        except FileNotFoundError:
            continue
        dest = arc / p.parent.parent.name / p.parent.name
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log("archive skipped, drive not writable:", e)
            return
        subprocess.run(["mv", str(p), str(dest / p.name)], stderr=subprocess.DEVNULL)
        rms = p.with_suffix(".rms.json")
        if rms.exists():
            subprocess.run(["mv", str(rms), str(dest / rms.name)], stderr=subprocess.DEVNULL)


def housekeep():
    """Free disk BEFORE cutting so renders never hit ENOSPC. Never raises: a missing drive must not stop clipping."""
    for step in (archive, prune):
        try:
            step()
        except Exception as e:
            log(step.__name__, "failed", str(e)[:200])
    stale = time.time() - 3600
    for base in (Path(os.environ.get("TMPDIR", "/tmp")), Path("/tmp")):
        for d in list(base.glob("gta-overlay-*")) + list(base.glob("hyperframes-*")):
            try:
                if d.stat().st_mtime < stale:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
    free = free_gb()
    if free < MIN_FREE_GB:
        notify(f"Mac disk down to {free:.0f}GB, overlays will be skipped until it clears {MIN_FREE_GB}GB")
    return free


def prune(dry=False):
    cutoff = time.time() - CFG["raw_keep_hours"] * 3600
    total = 0
    for base in (ROOT / "raw", Path(CFG["live"]), Path(CFG["archive"])):
        for d in base.glob("*/[0-9]*"):
            if not d.is_dir():
                continue
            for f in d.rglob("*"):
                if f.is_file() and f.stat().st_mtime < cutoff:
                    total += f.stat().st_size
                    if not dry:
                        f.unlink()
            if not dry:
                for sub in sorted(d.rglob("*"), reverse=True):
                    if sub.is_dir() and not any(sub.iterdir()):
                        sub.rmdir()
                if not any(d.iterdir()):
                    d.rmdir()
    if dry or total:
        print(f"{'would delete' if dry else 'deleted'} {total / 1e9:.1f}G of recordings older than {CFG['raw_keep_hours']}h")


def main():
    if sys.argv[1:] == ["--index"]:
        return index()
    if sys.argv[1:] == ["--prune-dry"]:
        return prune(dry=True)
    lock = open(HERE / "logs" / "highlight.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    if len(sys.argv) > 1:
        dirs = [Path(sys.argv[1])]
    else:
        dirs = [p.parent for r in (ROOT / "raw", Path(CFG["live"])) for p in r.glob("*/[0-9]*/meta.json") if time.time() - p.parent.stat().st_mtime < 2400]
    housekeep()
    for d in dirs:
        process(d)
    index()


if __name__ == "__main__":
    main()
