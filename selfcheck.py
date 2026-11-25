"""Check this machine can run the clipper. Prints PASS/FAIL per line, never prints key values.

python3 selfcheck.py              quick check
python3 selfcheck.py --claude     also proves `claude -p` works (uses a little usage)
python3 selfcheck.py --models     also downloads the whisper + background-removal models (first run, a few GB)
"""
import importlib, platform, shutil, subprocess, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
bad = 0


def check(ok, what, fix=""):
    global bad
    print(("PASS  " if ok else "FAIL  ") + what + ("" if ok or not fix else f"   -> {fix}"))
    bad += not ok


check(sys.version_info >= (3, 11), f"python {platform.python_version()}", "use Python 3.11+ (the .venv)")
check(platform.system() in ("Darwin", "Linux"), f"os {platform.system()}", "Mac, or Windows with WSL2 Ubuntu. Never plain Windows")
if platform.system() == "Linux":
    check(not str(HERE).startswith("/mnt/"), "repo lives inside Linux home", "move it to ~/ inside Ubuntu, never /mnt/c")

for mod in ["yaml", "requests", "numpy", "PIL", "cv2", "playwright", "rembg"]:
    try:
        importlib.import_module(mod)
        check(True, f"module {mod}")
    except Exception as e:
        check(False, f"module {mod}", f"pip install -r requirements.txt ({type(e).__name__})")
wh = "mlx_whisper" if platform.system() == "Darwin" else "faster_whisper"
try:
    importlib.import_module(wh)
    check(True, f"module {wh}")
except Exception:
    check(False, f"module {wh}", "pip install -r requirements.txt")

for tool in ["ffmpeg", "ffprobe", "yt-dlp", "node", "npx", "git", "claude"]:
    check(shutil.which(tool) is not None, f"command {tool}", "ask Claude to install it")

check((HERE / "models/face_detection_yunet_2023mar.onnx").exists(), "face model (YuNet)")
check(len(list((HERE / "fonts").glob("*.ttf"))) >= 6, "cover fonts")

cfg = None
if (HERE / "config.yaml").exists():
    import cfg as C
    cfg = C.load()
    check(True, "config.yaml")
    root = Path(cfg["root"])
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root).free / 1e9
    check(free > cfg.get("min_free_gb", 20) + 30, f"free disk at {root}: {free:.0f} GB", "need 50 GB+ free")
    check(any(cfg.get("accounts", {}).values()), "Blotato account ids in config.yaml", "python3 publish.py --accounts, then fill accounts:")
    check(bool(cfg.get("streamers")), "at least one streamer in config.yaml", "fine before day 3")
else:
    check(False, "config.yaml", "cp config.example.yaml config.yaml")

if (HERE / ".env").exists():
    import cfg as C
    for k in ["BLOTATO_API_KEY", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"]:
        check(bool(C.env(k)), f".env {k} is set", "fill it in .env by hand")
else:
    check(False, ".env", "cp .env.example .env, then fill in by hand")

try:
    urllib.request.urlopen("http://127.0.0.1:9333/json/version", timeout=2)
    check(True, "browser hub on :9333")
except Exception:
    check(False, "browser hub on :9333", "bash browser-hub/hub.sh start")

if "--claude" in sys.argv:
    r = subprocess.run(["claude", "-p", "reply with OK", "--strict-mcp-config", "--tools", ""], capture_output=True, text=True, timeout=180)
    check("OK" in r.stdout, "claude -p answers", "run `claude` in a terminal once and log in with your Max plan")

if "--models" in sys.argv:
    from rembg import new_session
    new_session("birefnet-portrait")
    check(True, "background-removal model downloaded")
    if platform.system() == "Darwin":
        import mlx_whisper  # noqa: F401
        from huggingface_hub import snapshot_download
        snapshot_download("mlx-community/whisper-large-v3-turbo")
    else:
        from faster_whisper import WhisperModel
        WhisperModel("large-v3-turbo", device="auto", compute_type="auto")
    check(True, "whisper model downloaded")

print(f"\n{'ALL PASS' if not bad else str(bad) + ' to fix'}")
sys.exit(1 if bad else 0)
