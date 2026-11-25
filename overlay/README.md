# GTA clip overlay

Transparent 1080x1920 30fps overlay (hook title, word-by-word captions, streamer handle) for vertical streamer clips. Renders to alpha webm. Another process cuts the video and composites.

## Use

```
python3 render_overlay.py clip.json out/overlay.webm
```

Non-zero exit on failure. Copies `template/` to a temp dir, injects the data, runs `npx hyperframes@0.7.100 render --format webm`. About 90s for a 20s clip (needs network on first run only to fetch the CLI).

## clip.json

```json
{
  "duration": 38.5,
  "hook": "XQC LOSES IT AT THE FIRST HEIST",
  "streamer": "xQc",
  "handle": "@xqc",
  "words": [{"w": "bro", "s": 0.42, "e": 0.61}]
}
```

- `duration`: seconds, also the render length. Words starting after it are dropped.
- `hook`: uppercased, max 2 lines, auto-shrinks (118px down). Visible the whole clip, pops in at 0s.
- `streamer`: used for the handle tag. `handle` is optional, defaults to `@` + lowercase streamer without spaces. Empty `streamer` hides the tag.
- `kick`: optional streamer URL (Kick streamers). Shows a full-width black banner with the Kick wordmark + URL at `kick_y` (default 600, the cam/gameplay seam) and hides the handle tag. highlight.py sets it for `platform: kick` and moves the hook to `hook_y` 730 under it.
- `words`: `w` text, `s` start, `e` end (seconds, clip-relative, sorted). Grouped 1-3 words (max 20 chars) per card. Breaks on sentence punctuation or a gap over 0.35s. Active word is yellow (#FFE14D), others white. A card holds until the next one if the gap is 0.8s or less, otherwise it hides 0.12s after its last word.

## Layout (safe zones)

- Hook: x 140-940, y 180-420. Captions: x 140-940, y 1240-1400. Handle: x 60, y 1500.
- Nothing in the right 140px or below y 1600. No guides are drawn.
- Fonts are self-hosted in `template/assets/fonts` (Anton for the hook, Montserrat Black for captions) under custom family names. GSAP is local in `template/assets`. No CDN.

## Composite

Alpha webm needs the libvpx-vp9 decoder forced, otherwise ffmpeg drops the alpha:

```
ffmpeg -i base.mp4 -c:v libvpx-vp9 -i overlay.webm \
  -filter_complex "[0:v][1:v]overlay=format=auto" \
  -map 0:a? -c:a copy -c:v libx264 -crf 18 -pix_fmt yuv420p out.mp4
```

Do not composite in Palmier (it applies alpha twice and darkens). Preview the template alone: `cd template && npx hyperframes@0.7.100 preview` (shows a built-in sample).

Verification frames from the 20s sample: `verify/frame_{1,8,15}s.png`. Sample data: `sample.clip.json`.
