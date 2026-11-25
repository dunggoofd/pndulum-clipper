"""Record one streamer while live on GTA: 60s video segments + chat log (Twitch IRC, Kick poll, YouTube via yt-dlp live_chat).

python3 rec.py <name>   (runs forever, launchd KeepAlive)
"""
import json, os, signal, socket, ssl, subprocess, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path

import requests, yaml

HERE = Path(__file__).parent
CFG = __import__("cfg").load()
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/130"}
TWITCH_GQL_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def status(name, s):
    if s["platform"] == "twitch":
        q = {"query": '{user(login:"%s"){stream{id title game{name}}}}' % s.get("login", name)}
        r = requests.post("https://gql.twitch.tv/gql", json=q, headers={"Client-ID": TWITCH_GQL_ID}, timeout=15)
        st = r.json()["data"]["user"]["stream"]
        if not st:
            return None
        return {"id": st["id"], "title": st["title"], "game": (st.get("game") or {}).get("name", "")}
    if s["platform"] == "youtube":
        r = subprocess.run(["yt-dlp", "--no-warnings", "--print", "%(is_live)s|%(id)s|%(title)s", s["url"]], capture_output=True, text=True, timeout=60)
        live, vid, title = (r.stdout.strip().split("|", 2) + ["", "", ""])[:3]
        if live != "True":
            return None
        return {"id": vid, "title": title, "game": "grand theft auto" if "gta" in title.lower() else title}
    d = requests.get(f"https://kick.com/api/v2/channels/{s.get('login', name)}", headers=UA, timeout=15).json()
    l = d.get("livestream")
    if not l:
        return None
    return {"id": str(l["id"]), "title": l["session_title"], "game": " ".join(c["name"] for c in l.get("categories", [])),
            "channel_id": d["id"]}


def twitch_chat(name, out, stop):
    ctx = ssl.create_default_context()
    sock = ctx.wrap_socket(socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=30), server_hostname="irc.chat.twitch.tv")
    sock.send(f"PASS SCHMOOPIIE\r\nNICK justinfan{int(time.time()) % 99999}\r\nJOIN #{name}\r\n".encode())
    buf = ""
    with open(out, "a") as f:
        while not stop.is_set():
            try:
                chunk = sock.recv(65536).decode(errors="ignore")
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split("\r\n")
            for ln in lines:
                if ln.startswith("PING"):
                    sock.send(b"PONG :tmi.twitch.tv\r\n")
                elif " PRIVMSG " in ln:
                    user = ln[1:ln.find("!")]
                    msg = ln.split(" :", 1)[1] if " :" in ln else ""
                    f.write(json.dumps({"t": round(time.time(), 2), "u": user, "m": msg}) + "\n")
            f.flush()
    sock.close()


def kick_chat(channel_id, out, stop):
    seen = set()
    url = f"https://kick.com/api/v2/channels/{channel_id}/messages"
    with open(out, "a") as f:
        while not stop.is_set():
            try:
                msgs = requests.get(url, headers=UA, timeout=10).json()["data"]["messages"]
            except Exception as e:
                log("kick chat err", e)
                time.sleep(5)
                continue
            ts = sorted(datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).timestamp() for m in msgs)
            span = max(ts[-1] - ts[0], 1) if ts else 0
            f.write(json.dumps({"t": round(time.time(), 2), "rate": round(len(ts) / span, 2) if ts else 0}) + "\n")
            for m in msgs:
                if m["id"] in seen:
                    continue
                seen.add(m["id"])
                t = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).timestamp()
                f.write(json.dumps({"t": t, "u": m["sender"]["username"], "m": m["content"]}) + "\n")
            if len(seen) > 5000:
                seen = set(m["id"] for m in msgs)
            f.flush()
            time.sleep(2)


def youtube_chat(vid, out, stop):
    tmp = out.parent / "ytchat"
    seen, p = set(), None
    with open(out, "a") as f:
        while not stop.is_set():
            if p is None or p.poll() is not None:
                subprocess.run(["rm", "-rf", str(tmp)])
                tmp.mkdir()
                p = subprocess.Popen(["yt-dlp", "-q", "--no-warnings", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                                      "-o", str(tmp / "lc.%(ext)s"), f"https://www.youtube.com/watch?v={vid}"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                src, pos, buf = tmp / "lc.live_chat.json.part", 0, b""
            time.sleep(2)
            if not src.exists():
                continue
            with open(src, "rb") as s:
                s.seek(pos)
                buf += s.read()
                pos = s.tell()
            *lines, buf = buf.split(b"\n")
            for ln in lines:
                try:
                    acts = json.loads(ln)["replayChatItemAction"]["actions"]
                except (json.JSONDecodeError, KeyError):
                    continue
                for a in acts:
                    it = a.get("addChatItemAction", {}).get("item", {})
                    r = it.get("liveChatTextMessageRenderer") or it.get("liveChatPaidMessageRenderer")
                    if not r or r["id"] in seen or "message" not in r:
                        continue
                    seen.add(r["id"])
                    msg = "".join(x.get("text") or (x["emoji"]["emojiId"] if len(x["emoji"].get("emojiId", "")) <= 4
                                                    else (x["emoji"].get("shortcuts") or [""])[0]) for x in r["message"]["runs"])
                    f.write(json.dumps({"t": round(int(r["timestampUsec"]) / 1e6, 2), "u": r["authorName"]["simpleText"].lstrip("@"), "m": msg}) + "\n")
            f.flush()
    if p and p.poll() is None:
        p.terminate()


def free_gb(path):
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    st = os.statvfs(p)
    return st.f_bavail * st.f_frsize / 1e9


def kill_group(proc, sig=signal.SIGKILL):
    """Signal the whole process group of a Popen started with start_new_session=True (yt-dlp and any helper it spawned)."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def record(name, s, st):
    day = datetime.now().strftime("%Y%m%d")
    vol = CFG.get("archive_volume")
    use_live = not vol or os.path.ismount(vol)
    if not use_live:
        log("archive volume not mounted, recording to the local raw dir")
    base = Path(CFG["live"]) if use_live else Path(CFG["root"]) / "raw"
    min_free = CFG.get("min_free_gb", 20)
    if free_gb(base) < min_free:
        log(f"only {free_gb(base):.0f}GB free on {base}, under the {min_free}GB floor, not recording")
        return False
    d = base / name / f"{day}-{st['id']}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "chat.jsonl").touch()
    (d / "meta.json").write_text(json.dumps({**st, "name": name, "platform": s["platform"], "started": time.time()}))
    stop = threading.Event()
    if s["platform"] == "twitch":
        th = threading.Thread(target=twitch_chat, args=(s.get("login", name), d / "chat.jsonl", stop), daemon=True)
    elif s["platform"] == "youtube":
        th = threading.Thread(target=youtube_chat, args=(st["id"], d / "chat.jsonl", stop), daemon=True)
    else:
        th = threading.Thread(target=kick_chat, args=(st["channel_id"], d / "chat.jsonl", stop), daemon=True)
    th.start()
    fmt = ["-f", f"bv*[height<={CFG['height']}]+ba", "--downloader", "ffmpeg"] if s["platform"] == "youtube" else ["-f", f"best[height<={CFG['height']}]"]
    # Each child gets its own session/process group so a stall-reconnect can kill yt-dlp, its ffmpeg downloader and the
    # segmenter outright. Killing only the direct children used to leave ffmpeg orphaned (ppid 1) and pulling the stream.
    ytd = subprocess.Popen(["yt-dlp", "-q", "--no-warnings", *fmt, "--hls-use-mpegts", "-o", "-", s["url"]], stdout=subprocess.PIPE,
                           start_new_session=True)
    ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c", "copy", "-f", "segment",
                           "-segment_time", str(CFG["segment_seconds"]), "-reset_timestamps", "1", "-strftime", "1",
                           str(d / "%s.ts")], stdin=ytd.stdout, start_new_session=True)
    ytd.stdout.close()
    log("recording", name, st["title"][:60], "->", d)
    try:
        record_loop(name, s, d, ytd, ff)
    finally:
        kill_group(ytd, signal.SIGTERM)   # EOF on ffmpeg's stdin lets it close the last segment cleanly
        try:
            ff.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        kill_group(ytd)                   # then make sure nothing from either tree survives
        kill_group(ff)
        ytd.wait()
        ff.wait()
        stop.set()
    log("segment pipe ended", name)
    return True


def record_loop(name, s, d, ytd, ff):
    last_check = started = time.time()
    while ff.poll() is None:
        time.sleep(5)
        if int(time.time() - started) % 30 < 5:
            newest = max((p.stat().st_mtime for p in d.glob("*.ts")), default=started)
            if time.time() - max(newest, started) > 180:
                log("no new segment for 3 min (network drop?), killing the pipe and reconnecting")
                break
            if free_gb(d) < CFG.get("min_free_gb", 20):
                log(f"under {CFG.get('min_free_gb', 20)}GB free on the recording disk, stopping")
                break
        if time.time() - last_check > 300:
            last_check = time.time()
            try:
                now = status(name, s)
            except Exception:
                continue
            from views import allowed
            if not allowed(name):
                log("dropped from the allocator's record list, stopping")
                break
            if not now or CFG["category_match"] not in now["game"].lower():
                log("left GTA or offline, stopping")
                break


def main():
    name = sys.argv[1]
    s = CFG["streamers"][name]
    while True:
        try:
            st = status(name, s)
        except Exception as e:
            log("status err", e)
            st = None
        from views import allowed
        if not allowed(name):
            time.sleep(600)
            continue
        if st and CFG["category_match"] in st["game"].lower():
            time.sleep(10 if record(name, s, st) else 300)   # False = disk under the free-space floor, check again in 5 min
        else:
            time.sleep(120)


if __name__ == "__main__":
    main()
