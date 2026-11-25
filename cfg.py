import os
from pathlib import Path

import yaml

HERE = Path(__file__).parent


def load():
    cfg = yaml.safe_load((HERE / "config.yaml").read_text())
    if os.environ.get("GTACLIP_ROOT"):
        cfg["root"] = os.environ["GTACLIP_ROOT"]
    for k in ("root", "live", "archive"):
        if cfg.get(k):
            cfg[k] = str(Path(cfg[k]).expanduser())
    return cfg


def env(key):
    f = HERE / ".env"
    if f.exists():
        for ln in f.read_text().splitlines():
            if ln.startswith(key + "="):
                return ln.split("=", 1)[1].strip().strip('"')
    return os.environ.get(key)


def notify(title, msg):
    import subprocess
    subprocess.run(["/bin/bash", str(HERE / "notify.sh"), title, msg])
