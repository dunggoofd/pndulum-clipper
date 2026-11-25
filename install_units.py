"""Install background jobs for this user (launchd on Mac, systemd --user on Linux/WSL). Installs only what you name.

python3 install_units.py --list
python3 install_units.py dash browserhub          always-on services
python3 install_units.py highlight views sentinel timers
python3 install_units.py rec:<streamer>           one recorder per streamer in config.yaml
python3 install_units.py --remove <job> ...
autopost and governor need --signed-off (Dung's sign-off after day 14).
"""
import argparse, os, platform, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
LOGS = HERE / "logs"
JOBS = {
    "dash": (["dash.py"], None),
    "browserhub": (["/bin/bash", "browser-hub/hub.sh", "run"], None),
    "highlight": (["highlight.py"], 300),
    "views": (["views.py"], 10800),
    "sentinel": (["sentinel.py"], 900),
    "allocator": (["allocator.py"], 3600),
    "scout": (["scout.py"], 10800),
    "streamclips": (["stream_clips.py"], 7200),
    "autopost": (["autopost.py"], 300),
    "governor": (["governor.py"], 900),
}
GATED = {"autopost", "governor"}
MAC = platform.system() == "Darwin"


def job(name):
    if name.startswith("rec:"):
        return ["rec.py", name[4:]], None
    if name not in JOBS:
        sys.exit(f"unknown job {name}. Try --list")
    return JOBS[name]


def argv(cmd):
    return cmd if cmd[0].startswith("/") else [PY, str(HERE / cmd[0]), *cmd[1:]]


def label(name):
    return "clipper." + name.replace(":", "-")


def path_env():
    return os.pathsep.join([str(Path(PY).parent), str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"])


def mac_install(name, cmd, every):
    lab, log = label(name), LOGS / f"{name.replace(':', '-')}.log"
    args = "".join(f"<string>{a}</string>" for a in argv(cmd))
    sched = f"<key>StartInterval</key><integer>{every}</integer>" if every else "<key>KeepAlive</key><true/>"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{lab}</string>
<key>ProgramArguments</key><array>{args}</array>
<key>WorkingDirectory</key><string>{HERE}</string>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>{path_env()}</string><key>HOME</key><string>{Path.home()}</string></dict>
{sched}<key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""
    dest = Path.home() / "Library/LaunchAgents" / f"{lab}.plist"
    dest.write_text(plist)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{lab}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(dest)], check=True)
    print("installed", lab)


def linux_install(name, cmd, every):
    unit, log = label(name).replace(".", "-"), LOGS / f"{name.replace(':', '-')}.log"
    d = Path.home() / ".config/systemd/user"
    d.mkdir(parents=True, exist_ok=True)
    exe = " ".join(f'"{a}"' for a in argv(cmd))
    svc = f"""[Unit]
Description={label(name)}

[Service]
Type={'oneshot' if every else 'simple'}
WorkingDirectory={HERE}
Environment="PATH={path_env()}"
Environment="HOME={Path.home()}"
ExecStart={exe}
StandardOutput=append:{log}
StandardError=append:{log}
{'TimeoutStartSec=infinity' if every else 'Restart=always' + chr(10) + 'RestartSec=10'}

[Install]
WantedBy=default.target
"""
    (d / f"{unit}.service").write_text(svc)
    if every:
        (d / f"{unit}.timer").write_text(f"""[Unit]
Description=Timer for {label(name)}

[Timer]
OnBootSec=60
OnUnitActiveSec={every}
Persistent=true
Unit={unit}.service

[Install]
WantedBy=timers.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"{unit}.{'timer' if every else 'service'}"], check=True)
    print("installed", unit)


def remove(name):
    lab = label(name)
    if MAC:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{lab}"], capture_output=True)
        (Path.home() / "Library/LaunchAgents" / f"{lab}.plist").unlink(missing_ok=True)
    else:
        unit = lab.replace(".", "-")
        for kind in ("timer", "service"):
            subprocess.run(["systemctl", "--user", "disable", "--now", f"{unit}.{kind}"], capture_output=True)
            (Path.home() / ".config/systemd/user" / f"{unit}.{kind}").unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"])
    print("removed", lab)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jobs", nargs="*")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--signed-off", action="store_true")
    a = ap.parse_args()
    if a.list or not a.jobs:
        for n, (c, e) in JOBS.items():
            print(f"{n:12} {'every ' + str(e) + 's' if e else 'always on'}{'   (needs --signed-off)' if n in GATED else ''}")
        print("rec:<name>   always on, one per streamer")
        return
    LOGS.mkdir(exist_ok=True)
    for n in a.jobs:
        if a.remove:
            remove(n)
            continue
        if n in GATED and not a.signed_off:
            sys.exit(f"{n} stays off until Dung signs off. Re-run with --signed-off once that is given.")
        (mac_install if MAC else linux_install)(n, *job(n))


if __name__ == "__main__":
    main()
