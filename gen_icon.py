"""Generate SnapCal's app icons (PWA + Play Store) — the signature open calorie ring + a fork,
on a sage gradient. Pure Pillow, no external assets. Outputs to static/icons/."""
import math
import os
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(__file__), "static", "icons")
os.makedirs(OUT, exist_ok=True)

TOP = (0x49, 0xC6, 0x8A)   # sage light
BOT = (0x12, 0x76, 0x50)   # sage deep
W = (255, 255, 255, 255)


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def make(size, maskable=False):
    s = size
    base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    for y in range(s):                                  # vertical sage gradient
        d.line([(0, y), (s, y)], fill=_lerp(TOP, BOT, y / s) + (255,))
    # rounded-square mask ('any' icon); maskable stays full-bleed square (OS masks it)
    mask = Image.new("L", (s, s), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, s - 1, s - 1], radius=(0 if maskable else int(s * 0.225)), fill=255)
    base.putalpha(mask)

    fg = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(fg)
    pad = s * (0.30 if maskable else 0.205)             # extra safe-zone for maskable
    rw = max(2, int(s * 0.072))                          # ring stroke
    box = [pad, pad, s - pad, s - pad]
    # open calorie ring (gap at the top — the app's signature), rounded ends
    d.arc(box, start=128, end=52, fill=W, width=rw)
    # the goal dot at 12 o'clock
    cx = s / 2.0
    dot = rw * 0.66
    d.ellipse([cx - dot, pad - dot, cx + dot, pad + dot], fill=W)

    # a clean fork in the center
    cy = s / 2.0
    fork_h = (s - 2 * pad) * 0.46
    handle_w = max(2, int(s * 0.035))
    top = cy - fork_h / 2.0
    bot = cy + fork_h / 2.0
    neck = top + fork_h * 0.42
    # handle (rounded)
    d.rounded_rectangle([cx - handle_w / 2, neck, cx + handle_w / 2, bot], radius=handle_w / 2, fill=W)
    # tines
    tine_w = max(2, int(s * 0.022))
    spread = s * 0.052
    for off in (-spread, 0, spread):
        d.rounded_rectangle([cx + off - tine_w / 2, top, cx + off + tine_w / 2, neck + handle_w * 0.2],
                            radius=tine_w / 2, fill=W)
    # crossbar joining the tines
    d.rounded_rectangle([cx - spread - tine_w / 2, neck - handle_w * 0.6, cx + spread + tine_w / 2, neck],
                        radius=handle_w * 0.3, fill=W)

    base.alpha_composite(fg)
    return base


def save(size, maskable=False, name=None):
    img = make(512, maskable=maskable).resize((size, size), Image.LANCZOS)
    name = name or ("icon-%d%s.png" % (size, "-maskable" if maskable else ""))
    p = os.path.join(OUT, name)
    img.save(p)
    print("wrote", p)


for sz in (192, 512):
    save(sz, maskable=False)
save(512, maskable=True)
save(512, maskable=False, name="icon-512.png")
# a 192 maskable too (some tools want it) + a flat 32 favicon
save(192, maskable=True)
make(512).resize((180, 180), Image.LANCZOS).save(os.path.join(OUT, "apple-touch-icon.png"))
print("apple-touch-icon written")
print("DONE")
