"""Generate the ThinkBuddy desktop-agent icon (green, light & lively).

Draws the badge natively in Pillow (no external source image needed) so the
 artwork is deterministic and brand-exact:

    green gradient badge (HanYang blue-badge DNA, greened)
      + white chat bubble w/ tail (Buddy = companion, negative-space glyph DNA)
      + green lightning bolt inside the bubble (Think = spark, bolt DNA)
      + lime->emerald energy arc, bottom-right (HanYang energy-arc DNA)
      + twin white sparkles, top-right (lightness)

Outputs (default, non-destructive):
    brand/logo/thinkbuddy-icon.png         1024px rounded-square master
    brand/logo/thinkbuddy-icon-circle.png  1024px circle badge variant
    brand/logo/thinkbuddy-icon.ico         multi-res 256/64/48/32/16

    python desktop-shell/tools/make_thinkbuddy_icon.py [--deploy]

With --deploy it additionally writes the canonical shell assets (512px):
    desktop-shell/assets/icon.png
    desktop-shell/assets/icon.ico
(picked up automatically by ThinkBuddyDesktop.spec / installer.iss on next build)
"""
from __future__ import annotations

import math
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

S = 4096          # supersample canvas (4x of 1024)
OUT = 1024        # master raster edge
ASSET = 512       # canonical shell asset edge (same as make_icon.py)
ICO_SIZES = [256, 64, 48, 32, 16]

ROOT = Path(__file__).resolve().parents[2]        # repo root (D:/studio/DeepTutor)
BRAND = ROOT / "brand" / "logo"
SHELL_ASSETS = ROOT / "desktop-shell" / "assets"

# ---- palette (绿色轻快) -------------------------------------------------
BG_TOP = (124, 240, 180)     # 7CF0B4
BG_MID = (47, 208, 143)      # 2FD08F  @ t=0.55
BG_BOT = (15, 174, 123)      # 0FAE7B
ARC_A = (217, 249, 157)      # D9F99D  (lime, near corner)
ARC_B = (52, 211, 153)       # 34D399  (emerald, outward)
BOLT = (13, 170, 118)        # 0DAA76
WHITE = (255, 255, 255)

# badge geometry (1024-space, scaled x4)
BX, BY, BS, BR = 64, 64, 896, 224


def _lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _bg3(t: float) -> tuple:
    """3-stop vertical-ish background gradient."""
    if t <= 0.55:
        return _lerp(BG_TOP, BG_MID, t / 0.55)
    return _lerp(BG_MID, BG_BOT, (t - 0.55) / 0.45)


def make_gradient(size: int, diagonal: bool) -> Image.Image:
    """Smooth gradient built small (cheap) then stretched; no edges to alias."""
    g = Image.new("RGB", (1, size)) if not diagonal else Image.new("RGB", (size, size))
    if diagonal:
        # project pixels onto the SVG gradient axis (128,96)->(896,928)
        ax, ay = 896 - 128, 928 - 96
        norm = ax * ax + ay * ay
        px = g.load()
        denom = float(size - 1)
        for y in range(size):
            py = (y / denom) * 1024
            for x in range(size):
                pxx = (x / denom) * 1024
                t = ((pxx - 128) * ax + (py - 96) * ay) / norm
                px[x, y] = _bg3(max(0.0, min(1.0, t)))
    else:
        px = g.load()
        denom = float(size - 1)
        for y in range(size):
            px[0, y] = _bg3(y / denom)
    return g.resize((S, S), Image.BILINEAR)


def star4(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill) -> None:
    """4-point sparkle (spikes up/right/down/left), concave via inner radius."""
    pts = []
    for i in range(8):
        ang = math.radians(-90 + i * 45)
        rr = r if i % 2 == 0 else r * 0.18
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    d.polygon(pts, fill=fill)


def render(shape: str = "square") -> Image.Image:
    k = S / 1024.0  # scale from design space

    # --- badge shape mask ---
    mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(mask)
    if shape == "circle":
        md.ellipse((BX * k, BY * k, (BX + BS) * k, (BY + BS) * k), fill=255)
    else:
        md.rounded_rectangle(
            (BX * k, BY * k, (BX + BS) * k, (BY + BS) * k), radius=BR * k, fill=255
        )

    # --- badge layer: everything below is clipped to the mask at the end ---
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = make_gradient(1024, diagonal=True)
    layer = Image.composite(grad.convert("RGBA"), layer, mask)

    d = ImageDraw.Draw(layer)

    # --- energy arc, bottom-right (segment-wise gradient + round caps) ---
    # The layer is clipped to the badge silhouette at the end, so the round
    # caps sit flush with the badge edge (HanYang energy-arc DNA: the arc
    # grows out of the rim, nothing floats outside the silhouette).
    cx = cy = (BX + BS) * k  # corner point (960,960)
    r_main, w_main = 380 * k, 40 * k
    segs = 30
    axis = (-380.0 * k, -380.0 * k)  # corner -> (580,580), matches SVG axis
    anorm = axis[0] ** 2 + axis[1] ** 2
    for i in range(segs):
        a0 = 180 + (90 / segs) * i
        a1 = 180 + (90 / segs) * (i + 1) + 0.6
        mid = math.radians(180 + (90 / segs) * (i + 0.5))
        # gradient position = projection of midpoint onto SVG axis
        px_, py_ = cx + r_main * math.cos(mid), cy + r_main * math.sin(mid)
        t = ((px_ - cx) * axis[0] + (py_ - cy) * axis[1]) / anorm
        col = _lerp(ARC_A, ARC_B, max(0.0, min(1.0, t)))
        bbox = (cx - r_main, cy - r_main, cx + r_main, cy + r_main)
        d.arc(bbox, a0, a1, fill=col, width=int(w_main))
    # round caps (color = gradient value at the endpoints, t=0.5)
    cap_col = _lerp(ARC_A, ARC_B, 0.5)
    rr = w_main / 2
    cap_a = (cx - r_main, cy)   # 180 deg point (bottom edge)
    cap_b = (cx, cy - r_main)   # 270 deg point (right edge)
    for cap in (cap_a, cap_b):
        d.ellipse(
            (cap[0] - rr, cap[1] - rr, cap[0] + rr, cap[1] + rr), fill=cap_col
        )

    # --- Buddy: white chat bubble + tail ---
    d.rounded_rectangle(
        (262 * k, 246 * k, 762 * k, 666 * k), radius=140 * k, fill=WHITE
    )
    d.polygon(
        [(396 * k, 650 * k), (356 * k, 752 * k), (498 * k, 668 * k)], fill=WHITE
    )

    # --- Think: lightning bolt inside the bubble ---
    bolt = [
        (532, 320), (432, 490), (504, 490),
        (462, 600), (592, 440), (514, 440), (567, 320),
    ]
    d.polygon([(x * k, y * k) for x, y in bolt], fill=BOLT)

    # --- sparkles (top-right), alpha-blended on the badge layer ---
    overlay = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    star4(od, 796 * k, 196 * k, 48 * k, (255, 255, 255, 242))
    star4(od, 872 * k, 289 * k, 26 * k, (255, 255, 255, 178))
    layer = Image.alpha_composite(layer, overlay)

    # --- clip everything to the badge silhouette ---
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    canvas = Image.composite(layer, canvas, mask)

    return canvas.resize((OUT, OUT), Image.LANCZOS)


def main() -> None:
    deploy = "--deploy" in sys.argv
    BRAND.mkdir(parents=True, exist_ok=True)

    square = render("square")
    circle = render("circle")
    p_png = BRAND / "thinkbuddy-icon.png"
    p_cir = BRAND / "thinkbuddy-icon-circle.png"
    p_ico = BRAND / "thinkbuddy-icon.ico"
    square.save(p_png)
    circle.save(p_cir)
    with p_ico.open("wb") as f:
        square.resize((256, 256), Image.LANCZOS).save(
            f, format="ICO", sizes=[(s, s) for s in ICO_SIZES]
        )
    print(f"wrote {p_png}  ({OUT}px, rounded-square)")
    print(f"wrote {p_cir}  ({OUT}px, circle badge)")
    print(f"wrote {p_ico}  sizes={ICO_SIZES}")

    if deploy:
        SHELL_ASSETS.mkdir(exist_ok=True)
        square.resize((ASSET, ASSET), Image.LANCZOS).save(SHELL_ASSETS / "icon.png")
        shutil.copyfile(p_ico, SHELL_ASSETS / "icon.ico")
        print(f"deployed -> {SHELL_ASSETS / 'icon.png'} + icon.ico ({ASSET}px)")


if __name__ == "__main__":
    main()
