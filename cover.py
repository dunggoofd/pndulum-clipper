"""9:16 cover for a cut clip, styled per streamer from their own YouTube thumbnails.

python3 cover.py <clip.json>                    cover next to the clip (<name>.cover.jpg) + candidates
python3 cover.py <clip.json> --t 12.4           force the frame time (seconds into clip.ts)
python3 cover.py <clip.json> --text "..."       force the cover text
Real frame + text only: no AI imagery, no logos (Reach pools ban both).
"""
import io, json, subprocess, sys, tempfile, time, types
from pathlib import Path

import cv2, numpy as np, yaml
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from views import banner_brand, safe_text

HERE = Path(__file__).parent
CFG = __import__("cfg").load()
ROOT = Path(CFG["root"])
YUNET = HERE / "models" / "face_detection_yunet_2023mar.onnx"
FONTS = HERE / "fonts"
W, H = 1080, 1920
SAFE_TOP, SAFE_BOT = 270, 1650
YELLOW, GREEN, RED, BLUE = (255, 225, 60), (60, 230, 90), (235, 40, 45), (40, 150, 255)

STYLES = {
    "xqc": dict(grade=(1.05, 1.18, 0.98), tint=(0, 6, 12), face_w=900, face_at=(0.64, 1200), text_y=330,
                font=("Montserrat-Black.ttf", 132), case="upper", box_word=True,
                copy="1 to 3 words, ALL CAPS, the reaction or the absurd thing on screen. The last word gets a yellow highlight box."),
    "n3on": dict(grade=(1.4, 1.12, 1.08), tint=(10, 4, 0), face_w=980, face_at=(0.5, 1180), text_y=540,
                 font=("Poppins-Bold.ttf", 96), case="sentence", sign=True, pill="N3ON",
                 copy="A short question or claim written like a hand-held sign, 2 to 6 words, sentence case."),
    "adinross": dict(grade=(1.15, 1.25, 1.0), tint=(6, 0, 0), face_w=960, face_at=(0.5, 1020), text_y=1330,
                     font=("Anton-Regular.ttf", 170), case="upper", quote="'", fill=(255, 255, 255),
                     copy="A real quote said in the clip (from the transcript), 2 to 4 words, ALL CAPS, the most savage or funny line."),
    "lacy": dict(name="Lacy", verbatim=True, grade=(1.2, 1.1, 1.06), tint=(0, 0, 0), face_w=940, face_at=(0.56, 1000), text_y=1300,
                 font=("Montserrat-ExtraBold.ttf", 104), case="sentence", quote="“",
                 copy="Pick the variant that fits this moment best, then follow its rule.",
                 variants={
                     "pin": dict(pill_pin=True, face_at=(0.56, 1150), text_y=1700, shift_max=420, why="a moment tied to a place (a store, the beach, a club, the cops, a house)",
                                 rule="text = a real quote, 2 to 5 words, sentence case, ending in '..' if it trails off; tag = the in-game place in 1 to 2 words (default 'Vice City')."),
                     "review": dict(layout="review", face_at=(0.5, 1180), face_w=980, text_y=420,
                                    why="he judges or rates something (food, a car, a girl, a mission, a price)",
                                    rule="text = his verdict quote, 2 to 7 words, all lowercase, ends with '.'; tag = ''."),
                     "sign": dict(sign=True, font=("PermanentMarker-Regular.ttf", 112), case="sentence", quote="", face_at=(0.6, 1180), text_y=560,
                                  why="he declares, begs, dares or asks chat something",
                                  rule="text = the line as if written on a hand-held sign, 2 to 5 words, sentence case, no quote marks; tag = ''."),
                     "clock": dict(layout="clock", face_at=(0.44, 1020), face_w=1000, text_y=1400,
                                   why="he says a time, price, count or number that is the point of the moment",
                                   rule="text = ONLY that number exactly as said, written as digits ('3:00 AM', '$50,000', '100'); tag = ''."),
                     "clean": dict(layout="clean", face_at=(0.5, 980), face_w=1060, text_y=1400,
                                   why="the face and the scene already tell it, any words would spoil it",
                                   rule="text = ''; tag = ''."),
                 }),
    "marlon": dict(name="Marlon", verbatim=True, grade=(1.12, 1.06, 1.03), tint=(0, 0, 0), face_w=900, face_at=(0.5, 1150), text_y=1640,
                   shift_max=420, font=("Montserrat-ExtraBold.ttf", 92), case="sentence", quote="“", pill_pin=True, pin_default="Botswana",
                   copy="A real line Marlon or the person he talks to says in the clip, 2 to 6 words, sentence case, on the natural-diamond story. Calm and accurate, never clickbait, never mention lab-grown diamonds. Tag = the place in Botswana (default 'Botswana')."),
    "clix": dict(name="Clix", verbatim=True, grade=(1.3, 1.2, 1.04), tint=(0, 0, 8), face_w=1000, face_at=(0.72, 1180), text_y=300,
                 font=("Montserrat-Italic[wght].ttf", 136, 900), case="upper", num_green=True,
                 copy="A bold claim, 1 to 3 words, ALL CAPS, numbers and $ welcome (they go green)."),
    "tomo": dict(grade=(1.65, 1.3, 1.06), tint=(8, 0, 0), face_w=1060, face_at=(0.42, 1140), text_y=290,
                 font=("Montserrat-Black.ttf", 120), case="upper", hdr=True, label=True, name_tag="Tomo",
                 copy="A 1 to 3 word label for the wild thing on screen, ALL CAPS, like a price, level or object ('$1 CAR', 'LVL 999', 'WRONG HOUSE')."),
}


def font(spec, size=None):
    f = ImageFont.truetype(str(FONTS / spec[0]), size or spec[1])
    if len(spec) > 2:
        f.set_variation_by_axes([spec[2]])
    return f


def clip_source(rec):
    for base in (Path(CFG["live"]), ROOT / "raw", Path(CFG["archive"])):
        for p in base.glob(f"{rec['streamer']}/*/work/{rec['peak']}/clip.ts"):
            return p
    return None


def loudness(src, n):
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True).stdout
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    hop = 800
    r = np.array([np.sqrt(np.mean(a[i:i + hop] ** 2)) for i in range(0, max(len(a) - hop, 1), hop)]) + 1
    return np.convolve(r, np.ones(5) / 5, mode="same")


def faces(frame, cam):
    fh, fw = frame.shape[:2]
    if cam:
        x, y, w, h = int(cam[0] * fw), int(cam[1] * fh), int(cam[2] * fw), int(cam[3] * fh)
        roi = cv2.resize(frame[y:y + h, x:x + w], (w * 3, h * 3))
        det = cv2.FaceDetectorYN_create(str(YUNET), "", (w * 3, h * 3), 0.6)
        _, fs = det.detect(roi)
        return [] if fs is None else [(x + b[0] / 3, y + b[1] / 3, b[2] / 3, b[3] / 3, b[14]) for b in fs]
    det = cv2.FaceDetectorYN_create(str(YUNET), "", (fw, fh), 0.75)
    _, fs = det.detect(frame)
    return [] if fs is None else [tuple(b[:4]) + (b[14],) for b in fs if b[3] > fh * 0.06]


def candidates(src, cam, step=0.4, keep=8, trim=0.0):
    cap = cv2.VideoCapture(str(src))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    loud = loudness(src, n)
    rows = []
    for t in np.arange(0.5, n / fps - 0.5, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
        ok, f = cap.read()
        if not ok:
            continue
        f = f[:int(f.shape[0] * (1 - trim))]
        fs = faces(f, cam)
        if not fs:
            continue
        x, y, w, h, c = max(fs, key=lambda b: b[2] * b[3])
        g = cv2.cvtColor(f[int(y):int(y + h), int(x):int(x + w)], cv2.COLOR_BGR2GRAY)
        sharp = cv2.Laplacian(g, cv2.CV_64F).var() if g.size else 0
        rows.append(dict(t=round(float(t), 2), face=(x, y, w, h), conf=float(c), sharp=sharp,
                         loud=float(loud[min(int(t * 10), len(loud) - 1)]), frame=f))
    cap.release()
    if not rows:
        return []
    z = lambda k: (lambda v: (v - v.mean()) / (v.std() + 1e-6))(np.array([r[k] for r in rows]))
    for r, s in zip(rows, z("loud") + 0.5 * z("sharp") + 0.3 * z("conf")):
        r["score"] = float(s)
    rows.sort(key=lambda r: -r["score"])
    out = []
    for r in rows:
        if all(abs(r["t"] - o["t"]) > 1.2 for o in out):
            out.append(r)
        if len(out) == keep:
            break
    return out


_SESSION = None


def cutout(img):
    global _SESSION
    if "lzma" not in sys.modules:
        m = types.ModuleType("lzma")
        class _E(Exception): pass
        class _F(io.BufferedIOBase):
            def __init__(s, *a, **k): raise _E()
        for k in ("FORMAT_AUTO", "FORMAT_XZ", "FORMAT_ALONE", "FORMAT_RAW", "CHECK_NONE", "CHECK_CRC64", "PRESET_DEFAULT", "FILTER_LZMA2"):
            setattr(m, k, 0)
        m.LZMAError, m.LZMAFile, m.LZMACompressor, m.LZMADecompressor = _E, _F, _F, _F
        sys.modules["lzma"] = m
    from rembg import new_session, remove
    _SESSION = _SESSION or new_session("birefnet-portrait")
    return remove(img, session=_SESSION)


def head_crop(frame, face, cam):
    fh, fw = frame.shape[:2]
    x, y, w, h = face
    cx, cy = x + w / 2, y + h / 2
    cw, ch = w * 3.1, w * 3.1 * 1.15
    x0, y0, x1, y1 = cx - cw / 2, cy - ch * 0.4, cx + cw / 2, cy + ch * 0.6
    if cam:
        bx, by, bw, bh = cam[0] * fw, cam[1] * fh, cam[2] * fw, cam[3] * fh
        x0, y0, x1, y1 = max(x0, bx), max(y0, by), min(x1, bx + bw), min(y1, by + bh)
    x0, y0, x1, y1 = int(max(x0, 0)), int(max(y0, 0)), int(min(x1, fw)), int(min(y1, fh))
    crop = Image.fromarray(cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2RGB))
    s = 900 / max(crop.size)
    crop = crop.resize((int(crop.width * s), int(crop.height * s)), Image.LANCZOS)
    return crop.filter(ImageFilter.UnsharpMask(2, 90, 2)), ((cx - x0) * s, (cy - y0) * s, w * s)


def grade(im, sat, con, bri, tint=(0, 0, 0), hdr=False):
    if hdr:
        blur = im.filter(ImageFilter.GaussianBlur(24))
        a, b = np.asarray(im, np.float32), np.asarray(blur, np.float32)
        im = Image.fromarray(np.clip(a + (a - b) * 0.9, 0, 255).astype(np.uint8))
    im = ImageEnhance.Contrast(ImageEnhance.Color(im).enhance(sat)).enhance(con)
    im = ImageEnhance.Brightness(im).enhance(bri)
    if any(tint):
        a = np.asarray(im, np.int16) + np.array(tint, np.int16)
        im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    return im


def background(frame, st):
    fh, fw = frame.shape[:2]
    gw = int(fh * W / H)
    g = cv2.cvtColor(frame[:, (fw - gw) // 2:(fw + gw) // 2], cv2.COLOR_BGR2RGB)
    a = g.astype(np.float32) / 255
    lum = a.mean(axis=2)
    lo, hi = np.percentile(lum, 1), np.percentile(lum, 99.5)
    a = np.clip((a - lo) / max(hi - lo, 0.2), 0, 1)
    m = float(np.clip(a.mean(), 0.05, 0.9))
    a = a ** (np.log(0.42) / np.log(m)) if m < 0.36 else a
    g = cv2.bilateralFilter((a * 255).astype(np.uint8), 5, 30, 5)
    im = Image.fromarray(g).resize((W, H), Image.LANCZOS).filter(ImageFilter.UnsharpMask(2, 70, 3))
    im = grade(im, *st["grade"], st.get("tint", (0, 0, 0)), st.get("hdr", False))
    v = np.ones((H, W), np.float32)
    yy, xx = np.mgrid[0:H, 0:W]
    v -= 0.55 * np.clip(((xx - W / 2) / (W * 0.75)) ** 2 + ((yy - H * 0.45) / (H * 0.7)) ** 2, 0, 1)
    return Image.fromarray((np.asarray(im, np.float32) * v[..., None]).astype(np.uint8))


def stroked_cut(cut, stroke=16, shadow=30):
    a = cut.split()[-1]
    a = a.filter(ImageFilter.GaussianBlur(1.5)).point(lambda p: 0 if p < 70 else 255 if p > 170 else int((p - 70) * 2.55))
    cut.putalpha(a)
    ring = a.filter(ImageFilter.MaxFilter(stroke * 2 + 1)).filter(ImageFilter.GaussianBlur(1.5))
    pad = shadow * 2
    out = Image.new("RGBA", (cut.width + pad * 2, cut.height + pad * 2), (0, 0, 0, 0))
    sh = Image.new("RGBA", out.size, (0, 0, 0, 0))
    sh.paste((0, 0, 0, 200), (pad + 10, pad + 18), ring)
    out = Image.alpha_composite(out, sh.filter(ImageFilter.GaussianBlur(shadow)))
    out.paste((255, 255, 255, 255), (pad, pad), ring)
    out.alpha_composite(cut, (pad, pad))
    return out


def draw_text(base, st, text, highlight=""):
    f = font(st["font"])
    text = text.upper() if st["case"] == "upper" else text
    if st.get("quote") == "'":
        text = f"‘{text}’"
    elif st.get("quote"):
        text = f"“{text}”"
    words = text.split()
    d0 = ImageDraw.Draw(base)
    maxw = W - 120
    lines, cur = [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d0.textlength(t, font=f) > maxw and cur:
            lines.append(cur)
            cur = w
        else:
            cur = t
    lines.append(cur)
    while max(d0.textlength(ln, font=f) for ln in lines) > maxw:
        f = font(st["font"], int(f.size * 0.92))
    lh = int(f.size * 1.08)
    y = st["text_y"] - (len(lines) - 1) * lh // 2
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    sw = max(6, f.size // 14)
    hl = (highlight or "").upper() if st["case"] == "upper" else (highlight or "")
    for i, ln in enumerate(lines):
        tw = d.textlength(ln, font=f)
        x = (W - tw) / 2
        if st.get("box_word") and i == len(lines) - 1:
            last = ln.split()[-1]
            pre = ln[: len(ln) - len(last)]
            px = x + d.textlength(pre, font=f)
            lw = d.textlength(last, font=f)
            d.rounded_rectangle((px - 18, y - 6, px + lw + 18, y + f.size * 1.02), radius=14, fill=YELLOW, outline=(0, 0, 0), width=6)
            d.text((x, y), pre, font=f, fill=(255, 255, 255), stroke_width=sw, stroke_fill=(0, 0, 0))
            d.text((px, y), last, font=f, fill=(0, 0, 0))
        else:
            cx = x
            for w in ln.split(" "):
                fill = st.get("fill", (255, 255, 255))
                core = w.strip("‘’“”.,!?")
                if st.get("num_green") and any(ch.isdigit() or ch == "$" for ch in core):
                    fill = GREEN
                elif hl and core == hl.strip(".,!?"):
                    fill = YELLOW
                d.text((cx, y), w, font=f, fill=fill, stroke_width=sw, stroke_fill=(0, 0, 0))
                cx += d.textlength(w + " ", font=f)
        y += lh
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh.paste((0, 0, 0, 170), (8, 12), layer.split()[-1])
    base.alpha_composite(sh.filter(ImageFilter.GaussianBlur(10)))
    base.alpha_composite(layer)
    top = st["text_y"] - (len(lines) - 1) * lh // 2
    return (top, top + len(lines) * lh)


def draw_sign(base, st, text, top=0):
    f = font(st["font"])
    d = ImageDraw.Draw(base)
    def wrap(maxw):
        lines, cur = [], ""
        for w in text.split():
            t = (cur + " " + w).strip()
            if d.textlength(t, font=f) > maxw and cur:
                lines.append(cur)
                cur = w
            else:
                cur = t
        return lines + [cur]
    lines = min((wrap(mw) for mw in range(480, 841, 20)),
                key=lambda ls: (len(ls), max(d.textlength(l, font=f) for l in ls) - min(d.textlength(l, font=f) for l in ls)))
    lh = int(f.size * 1.18)
    bw = max(d.textlength(ln, font=f) for ln in lines) + 90
    bh = len(lines) * lh + 60
    x0, y0 = (W - bw) / 2, max(st["text_y"] - bh / 2, top)
    card = Image.new("RGBA", (int(bw) + 80, int(bh) + 80), (0, 0, 0, 0))
    cd = ImageDraw.Draw(card)
    cd.rounded_rectangle((40, 40, 40 + bw, 40 + bh), radius=18, fill=(255, 255, 255))
    for i, ln in enumerate(lines):
        bb = cd.textbbox((0, 0), ln, font=f)
        cd.text((40 + (bw - (bb[2] - bb[0])) / 2 - bb[0], 70 + i * lh + (lh - (bb[3] - bb[1])) / 2 - bb[1] - 8), ln, font=f, fill=(15, 15, 15))
    sh = Image.new("RGBA", card.size, (0, 0, 0, 0))
    sh.paste((0, 0, 0, 150), (6, 14), card.split()[-1])
    card = Image.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14)), card).rotate(-3, resample=Image.BICUBIC, expand=True)
    base.alpha_composite(card, (int(x0 - 40), int(y0 - 40)))


def verified_pill(base, name, xy, avatar=None):
    f = font(("Poppins-Bold.ttf", 58))
    d = ImageDraw.Draw(base)
    tw = d.textlength(name, font=f)
    x, y = xy
    h = 96
    w = h + tw + 110
    d.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=(255, 255, 255), outline=(20, 20, 20), width=4)
    if avatar and avatar.exists():
        av = Image.open(avatar).convert("RGB").resize((h - 16, h - 16))
        m = Image.new("L", av.size, 0)
        ImageDraw.Draw(m).ellipse((0, 0, av.width, av.height), fill=255)
        base.paste(av, (x + 8, y + 8), m)
    d.text((x + h + 8, y + 10), name, font=f, fill=(15, 15, 15))
    cx, cy, r = x + h + tw + 48, y + h / 2, 26
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=BLUE)
    d.line([(cx - 12, cy), (cx - 3, cy + 10), (cx + 13, cy - 10)], fill=(255, 255, 255), width=7, joint="curve")


KICK_LOGO = ROOT / "style-refs" / "_brand" / "kick-logo.png"


def stars(d, x, y, r=22, n=5):
    for i in range(n):
        cx = x + r + i * r * 2.3
        pts = [(cx + (r if k % 2 == 0 else r * 0.45) * np.sin(k * np.pi / 5), y + r - (r if k % 2 == 0 else r * 0.45) * np.cos(k * np.pi / 5)) for k in range(10)]
        d.polygon(pts, fill=(255, 196, 0))


def review_card(base, name, text, y, avatar=None):
    fq, fn = font(("Montserrat-SemiBold.ttf", 66)), font(("Poppins-Bold.ttf", 54))
    d = ImageDraw.Draw(base)
    maxw, lines, cur = 760, [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=fq) > maxw and cur:
            lines.append(cur)
            cur = w
        else:
            cur = t
    lines.append(cur)
    lh = int(fq.size * 1.2)
    cw = max(620, max(d.textlength(l, font=fq) for l in lines) + 100)
    ch = 150 + len(lines) * lh + 30
    card = Image.new("RGBA", (int(cw) + 80, int(ch) + 80), (0, 0, 0, 0))
    cd = ImageDraw.Draw(card)
    cd.rounded_rectangle((40, 40, 40 + cw, 40 + ch), radius=30, fill=(255, 255, 255))
    if avatar and avatar.exists():
        av = Image.open(avatar).convert("RGB").resize((96, 96))
        m = Image.new("L", av.size, 0)
        ImageDraw.Draw(m).ellipse((0, 0, 96, 96), fill=255)
        card.paste(av, (88, 72), m)
    cd.text((204, 66), name, font=fn, fill=(15, 15, 15))
    stars(cd, 204, 136, 18)
    for i, ln in enumerate(lines):
        cd.text((90, 190 + i * lh), ln, font=fq, fill=(20, 20, 20))
    sh = Image.new("RGBA", card.size, (0, 0, 0, 0))
    sh.paste((0, 0, 0, 150), (6, 14), card.split()[-1])
    card = Image.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14)), card)
    base.alpha_composite(card, (int((W - cw) / 2 - 40), int(y - 40)))
    return (y, y + ch)


def led_stamp(base, text, xy=(None, 150)):
    f = font(("DINNextW1G-Black.otf", 170))
    d = ImageDraw.Draw(base)
    bb = d.textbbox((0, 0), text, font=f)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    x = W - tw - 130 if xy[0] is None else xy[0]
    y = xy[1]
    d.rounded_rectangle((x - 50, y - 40, x + tw + 50, y + th + 50), radius=20, fill=(12, 10, 14), outline=(60, 20, 20), width=4)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).text((x - bb[0], y - bb[1]), text, font=f, fill=(255, 30, 30, 255))
    base.alpha_composite(glow.filter(ImageFilter.GaussianBlur(18)))
    base.alpha_composite(glow.filter(ImageFilter.GaussianBlur(5)))
    d.text((x - bb[0], y - bb[1]), text, font=f, fill=(255, 70, 60))
    return (y - 40, y + th + 50)


def recent_variants(rec, n=2):
    rows = []
    for p in (ROOT / "clips").glob(f"*/{rec['streamer']}-*.json"):
        if p.name.endswith(".cover.json") or p.stem == rec.get("file", "").rsplit("/", 1)[-1].rsplit(".", 1)[0]:
            continue
        try:
            c = json.loads(p.read_text()).get("cover") or {}
        except ValueError:
            continue
        if c.get("variant"):
            rows.append((c.get("made", 0), c["variant"]))
    return [v for _, v in sorted(rows, reverse=True)[:n]]


def kick_banner(base, url, y, h=112, brand="kick"):
    d = ImageDraw.Draw(base)
    d.rectangle((0, y, W, y + h), fill=(0, 0, 0))
    logo = Image.open(KICK_LOGO.with_name(f"{brand}-logo.png")).convert("RGBA")
    lh = int(h * 0.86)
    logo = logo.resize((int(logo.width * lh / logo.height), lh), Image.LANCZOS)
    base.alpha_composite(logo, (60, y - int(h * 0.12) if brand == "kick" else y + (h - lh) // 2))
    label = url.split("://")[-1].rstrip("/").upper()
    f = font(("Anton-Regular.ttf", 54))
    bb = d.textbbox((0, 0), label, font=f)
    cx = 60 + logo.width + (W - 60 - logo.width) / 2
    d.text((cx - (bb[2] - bb[0]) / 2 - bb[0], y + (h - (bb[3] - bb[1])) / 2 - bb[1]), label, font=f, fill=(255, 255, 255))


def pin_pill(base, place, xy):
    f = font(("Montserrat-ExtraBold.ttf", 56))
    d = ImageDraw.Draw(base)
    tw = d.textlength(place, font=f)
    x, y = xy
    d.rounded_rectangle((x, y, x + tw + 120, y + 90), radius=16, fill=(255, 255, 255))
    px, py = x + 46, y + 38
    d.ellipse((px - 20, py - 22, px + 20, py + 18), fill=RED)
    d.polygon([(px - 17, py + 4), (px + 17, py + 4), (px, py + 36)], fill=RED)
    d.ellipse((px - 8, py - 10, px + 8, py + 6), fill=(255, 255, 255))
    d.text((x + 84, y + 12), place, font=f, fill=(15, 15, 15))


def label_pill(base, text, xy):
    f = font(("Montserrat-Black.ttf", 92))
    d = ImageDraw.Draw(base)
    tw = d.textlength(text, font=f)
    x, y = xy
    x = min(x, W - tw - 110)
    d.rounded_rectangle((x, y, x + tw + 70, y + 130), radius=28, fill=(18, 18, 18), outline=(255, 255, 255), width=6)
    d.text((x + 35, y + 10), text, font=f, fill=GREEN)


def name_tag(base, name, xy):
    f = font(("Montserrat-ExtraBold.ttf", 60))
    ImageDraw.Draw(base).text(xy, name, font=f, fill=(255, 255, 255), stroke_width=6, stroke_fill=(0, 0, 0))


def face_grid(cands, cam):
    tiles = []
    for i, c in enumerate(cands):
        im, _ = head_crop(c["frame"], c["face"], cam)
        im = im.resize((300, int(300 * im.height / im.width)))
        t = Image.new("RGB", (300, 360), (0, 0, 0))
        t.paste(im.crop((0, 0, 300, min(360, im.height))), (0, 0))
        d = ImageDraw.Draw(t)
        d.rectangle((0, 0, 58, 58), fill=(255, 255, 0))
        d.text((14, 2), str(i + 1), font=font(("Montserrat-Black.ttf", 48)), fill=(0, 0, 0))
        tiles.append(t)
    g = Image.new("RGB", (300 * 4, 360 * ((len(tiles) + 3) // 4)))
    for i, t in enumerate(tiles):
        g.paste(t, ((i % 4) * 300, (i // 4) * 360))
    p = Path(tempfile.gettempdir()) / f"cover-grid-{id(cands)}.jpg"
    g.save(p, quality=85)
    return p


def copy_for(rec, st, grid=None, allowed=None):
    words = rec.get("words", "")
    hooks = rec.get("judge", {}).get("hooks", [])
    look = f"""First Read the image {grid}. It shows numbered face crops of the streamer from this clip.
Pick the ONE that makes the best thumbnail face: biggest emotion (mouth open, eyes wide, hand on head/mouth, laughing hard, rage),
eyes open, not mid-blink, not blurred, face not covered by a mic. A neutral or looking-down face is the worst pick.
Return its number as "pick".
""" if grid else ""
    rules = ""
    if allowed:
        rules += "\nCover variants you may use (pick ONE, return it as \"variant\"):" + "".join(
            f"\n- {k}: use when {st['variants'][k]['why']}. {st['variants'][k]['rule']}" for k in allowed)
        rules += f"\nThe name {st['name']} is drawn on the cover separately, do not put it in the text."
    elif st.get("name"):
        rules += f"\nThe text MUST contain the name {st['name']} (campaign rule: the overlay must name the streamer)."
    if st.get("verbatim"):
        rules += "\nUse ONLY words the streamer actually says in the transcript, verbatim (plus the name). No invented lines."
    prompt = look + f"""Write the text for a 9:16 TikTok/Shorts cover image of a {'clip from a sponsored vlog' if CFG['streamers'].get(rec['streamer'], {}).get('brief') else 'GTA VI stream clip'} of {rec['streamer']}.
Burned-in hooks already on the video: {hooks}
Transcript: {words[:1200]}
Cover text rule: {st['copy']}{rules}
The cover must make someone stop scrolling and tap. Curiosity, not a spoiler. No emojis. Do not repeat the burned-in hook word for word.
Never use words that trip TikTok/IG/YouTube moderation on a cover: kill/murder/die/dead/suicide, shoot/gun/weapons, blood, bomb, drugs, sex acts, nudity, slurs, swearing. Swap for a safe word that keeps the curiosity (unalived, cooked, took out, pew pew). "Kill" in any form always becomes "unalived".
Also give "highlight": one word from the text to colour, and "tag": a 1 to 3 word extra label specific to this moment (or ""). Never "GTA 6" or "GTA VI" as the tag.
Reply ONLY JSON: {{"pick": number, "text": "...", "highlight": "...", "tag": "..."{', "variant": "..."' if allowed else ''}}}"""
    tools = ["--tools", "Read", "--allowedTools", "Read"] if grid else ["--tools", ""]
    cmd = ["claude", "-p", prompt, "--model", "sonnet", "--strict-mcp-config", *tools, "--system-prompt", "You output only JSON.",
           "--disable-slash-commands", "--no-chrome", "--no-session-persistence", "--output-format", "json"]
    for attempt in range(3):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=tempfile.gettempdir(), timeout=180)
        try:
            txt = json.loads(r.stdout)["result"]
            out = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
            if out.get("text") or out.get("variant") == "clean":
                break
        except (ValueError, KeyError, TypeError):
            if attempt == 2:
                raise
    if grid:
        grid.unlink(missing_ok=True)
    return out


def choose(rec, st, cands, cam):
    allowed = None
    if st.get("variants"):
        last = recent_variants(rec)
        allowed = [k for k in st["variants"] if k not in last] or list(st["variants"])
    copy = copy_for(rec, st, face_grid(cands, cam), allowed)
    if allowed:
        v = copy.get("variant")
        if v not in allowed or (v == "clock" and not any(ch.isdigit() for ch in copy.get("text", ""))):
            v = next(k for k in allowed if k != "clock")
            copy["text"] = copy.get("text", "") if v != "clean" else ""
        copy["variant"] = v
    copy["text"] = safe_text(copy.get("text") or "") or ""
    copy["tag"] = safe_text(copy.get("tag") or "") or ""
    k = int(copy.get("pick") or 1) - 1
    k = k if 0 <= k < len(cands) else 0
    cands.insert(0, cands.pop(k))
    return copy


def render(rec, cand, copy, cam):
    st = STYLES.get(rec["streamer"], STYLES["xqc"])
    if st.get("variants"):
        st = {**st, **st["variants"].get(copy.get("variant") or "pin", st["variants"]["pin"])}
    meta = CFG["streamers"].get(rec["streamer"], {})
    lay = st.get("layout")
    st = {**st, "text_y": min(max(st["text_y"], SAFE_TOP + 200), SAFE_BOT - 180)}
    base = background(cand["frame"], st).convert("RGBA")
    head, (hx, hy, hw) = head_crop(cand["frame"], cand["face"], cam)
    head = grade(head, min(st["grade"][0], 1.35), 1.08, 1.04, hdr=st.get("hdr", False))
    cut = stroked_cut(cutout(head))
    pad = (cut.width - head.width) // 2
    fx, fy = st["face_at"]
    s = st["face_w"] * 0.36 / hw
    body = lambda k: cut.height * k - pad * k - (pad + hy) * k
    top_min = st["text_y"] + 230 if st["text_y"] < H / 2 else 0
    y = int(fy - (pad + hy) * s)
    y = max(y, top_min)
    shift = H + 20 - (y + body(s) + (pad + hy) * s)
    if shift > 0:
        y += int(min(shift, st.get("shift_max", 150)))
    cut = cut.resize((int(cut.width * s), int(cut.height * s)), Image.LANCZOS)
    x = int(fx * W - (pad + hx) * s)
    x = max(0, min(x, W - cut.width))
    if st.get("sign"):
        draw_sign(base, st, copy["text"], 400 if banner_brand(meta) else 0)
    base.alpha_composite(cut, (x, y))
    if lay == "review":
        ty = review_card(base, st["name"], copy["text"].lower(), 150, ROOT / "style-refs" / rec["streamer"] / "art" / "profile")
    elif lay == "clock":
        ty = led_stamp(base, copy["text"])
    elif lay == "clean":
        ty = (0, 0)
    elif not st.get("sign"):
        ty = draw_text(base, st, copy["text"], copy.get("highlight"))
    else:
        ty = (st["text_y"] - 120, st["text_y"] + 120)
    tag = (copy.get("tag") or "").strip()
    if tag.upper().replace(" ", "") in ("GTA6", "GTAVI", "GTA", "VICECITY") and not st.get("pill_pin"):
        tag = ""
    brand = banner_brand(meta)
    if brand:
        kick_banner(base, meta["url"], 250, brand=brand)
    elif st.get("pill"):
        verified_pill(base, st["pill"], (70, 110), ROOT / "style-refs" / rec["streamer"] / "art" / "profile")
    if st.get("pill_pin"):
        pin_pill(base, tag or st.get("pin_default", "Vice City"), (70, 160))
    if st.get("label") and tag:
        label_pill(base, tag.upper(), (W - 560, max(y - 150, ty[1] + 30)))
    tag_name = st.get("name_tag") or (st.get("name") if st.get("name", "").lower() not in copy["text"].lower() else None)
    if tag_name and lay != "review":
        name_tag(base, tag_name, (max(x + 140, 60), y - 10))
    return base.convert("RGB")


def make(clip_json, t=None, text=None):
    clip_json = Path(clip_json)
    rec = json.loads(clip_json.read_text())
    src = clip_source(rec)
    if src is None:
        raise SystemExit(f"no clip.ts for {rec['streamer']} {rec['peak']}")
    sc = CFG["streamers"][rec["streamer"]]
    cam = sc.get("cam") or sc.get("cover_cam")
    cands = candidates(src, cam, trim=sc.get("trim_bottom", 0.0))
    if not cands:
        raise SystemExit("no face found in clip")
    st = STYLES.get(rec["streamer"], STYLES["xqc"])
    prev = {k: v for k, v in (rec.get("cover") or {}).items() if k in ("text", "highlight", "tag", "variant")}
    if t is not None:
        cands.insert(0, cands.pop(min(range(len(cands)), key=lambda i: abs(cands[i]["t"] - t))))
    if text:
        copy = {"highlight": "", "tag": "", **prev, "text": text}
    elif t is not None and prev:
        copy = prev
    else:
        copy = choose(rec, st, cands, cam)
    out = clip_json.with_suffix(".cover.jpg")
    render(rec, cands[0], copy, cam).save(out, quality=90)
    cd = clip_json.parent / "covers" / clip_json.stem
    cd.mkdir(parents=True, exist_ok=True)
    for f in cd.glob("*.jpg"):
        f.unlink()
    for c in cands:
        im, _ = head_crop(c["frame"], c["face"], cam)
        im.resize((200, int(200 * im.height / im.width))).save(cd / f"{c['t']}.jpg", quality=80)
    rec = json.loads(clip_json.read_text())
    rec["cover"] = {"file": out.name, "t": cands[0]["t"], "text": copy.get("text", ""), "highlight": copy.get("highlight", ""),
                    "tag": copy.get("tag", ""), "variant": copy.get("variant", ""), "candidates": [c["t"] for c in cands], "made": time.time()}
    clip_json.write_text(json.dumps(rec, indent=1))
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    t = float(a[a.index("--t") + 1]) if "--t" in a else None
    tx = a[a.index("--text") + 1] if "--text" in a else None
    print(make(a[0], t, tx))
