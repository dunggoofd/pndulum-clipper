# Pndulum clipper

Live stream in, approved 9:16 clips out, posted and submitted to clipping campaigns. Mac (Apple silicon) or Windows with WSL2 Ubuntu.

Follow the Clipping System SOP from Dung. Short version:

```bash
gh repo clone dunggoofd/pndulum-clipper ~/pndulum-clipper && cd ~/pndulum-clipper
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env        # fill in by hand
python3 selfcheck.py --models
python3 selfcheck.py --claude
bash browser-hub/hub.sh start
python3 install_units.py dash browserhub
```

Then open http://127.0.0.1:7779. Switch on the rest in the SOP's order, one at a time, with proof before the next:
`rec:<streamer>` -> `highlight` -> `views sentinel` -> `allocator` -> `scout streamclips`.

`autopost` and `governor` stay off until Dung signs off.

Tools the self-check wants on PATH: ffmpeg, ffprobe, yt-dlp, node + npx (HyperFrames overlays), git, gh, claude.
