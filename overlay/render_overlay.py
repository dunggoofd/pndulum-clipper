#!/usr/bin/env python3
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HF = "hyperframes@0.7.100"
MIN_FREE_GB = 10
WORKERS = 2   # 'auto' spawned 3+ Chrome workers each writing frame PNGs to TMPDIR; ENOSPC on 20 Nov. No source video, so the frame cache buys nothing.
TEMPLATE = Path(__file__).resolve().parent / "template"


def main():
    if len(sys.argv) != 3:
        print("usage: render_overlay.py <clip.json> <out.webm>", file=sys.stderr)
        return 2
    clip_path, out = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
    clip = json.loads(clip_path.read_text())
    for k in ("duration", "hook", "words"):
        if k not in clip:
            print(f"clip.json missing '{k}'", file=sys.stderr)
            return 2
    dur = float(clip["duration"])

    free = shutil.disk_usage(tempfile.gettempdir()).free / 1e9
    if free < MIN_FREE_GB:
        print(f"render skipped: {free:.0f}GB free in {tempfile.gettempdir()}, need {MIN_FREE_GB}", file=sys.stderr)
        return 3

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gta-overlay-") as tmp:
        work = Path(tmp) / "proj"
        shutil.copytree(TEMPLATE, work)
        (work / "clip.json").write_text(json.dumps(clip))
        html = (work / "index.html").read_text()
        blob = json.dumps(clip).replace("</", "<\\/")
        html = re.sub(
            r'(<script id="clip-data" type="application/json">).*?(</script>)',
            lambda m: m.group(1) + blob + m.group(2),
            html, count=1, flags=re.S,
        )
        html = re.sub(r'data-duration="20"', f'data-duration="{dur}"', html)
        (work / "index.html").write_text(html)

        tmp_out = work / "overlay.webm"
        cmd = ["npx", "--yes", HF, "render", ".", "--format", "webm", "--fps", "30",
               "--vp9-cpu-used", "5", "--workers", str(WORKERS), "--frames-cache-dir", "off",
               "-o", str(tmp_out), "--quiet"]
        r = subprocess.run(cmd, cwd=work)
        if r.returncode != 0 or not tmp_out.exists():
            print("render failed", file=sys.stderr)
            return r.returncode or 1
        shutil.copyfile(tmp_out, out)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
