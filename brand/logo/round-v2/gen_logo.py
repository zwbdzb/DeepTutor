"""ThinkBuddy logo — round v2 (corporate palette aligned with Tokengine VI).

Renders every variant to PNG (multiple sizes) + ICO. SVG is the source of truth.
"""
import os
import io

import resvg_py
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- palette
# Sampled from https://tokengine.hanyoai.com/favicon.ico
NAVY = "#143790"      # deep blue (left wing of the corporate mark)
BLUE = "#036DB7"      # primary corporate blue
SKY = "#1E86CE"       # lighter blue
YELLOW = "#FCDC2A"    # corporate accent (lightning)
ORANGE = "#F0722A"    # warm end of the corporate gradient

BADGE = (
    "M 240 64 H 784 A 176 176 0 0 1 960 240 V 784 A 176 176 0 0 1 784 960 "
    "H 240 A 176 176 0 0 1 64 784 V 240 A 176 176 0 0 1 240 64 Z"
)

# Compass / question-mark glyph (centre-line path, drawn with a round stroke)
QMARK = (
    "M 400 410 C 400 348.1 450.1 298 512 298 C 573.9 298 624 348.1 624 410 "
    "C 624 462 596 510 546 522 C 526 526 512 546 512 576"
)


def _svg_defs(colors):
    """colors: list of (id, stops) where stops = [(offset, hex), ...]"""
    out = []
    for cid, (x1, y1, x2, y2), stops in colors:
        s = "".join(
            f'<stop offset="{o}" stop-color="{c}"/>' for o, c in stops
        )
        out.append(
            f'<linearGradient id="{cid}" x1="{x1}" y1="{y1}" x2="{x2}" '
            f'y2="{y2}" gradientUnits="userSpaceOnUse">{s}</linearGradient>'
        )
    return "<defs>" + "".join(out) + "</defs>"


def qmark_group(fill, dot_fill, sw=112):
    return (
        f'<g fill="none" stroke="{fill}" stroke-width="{sw}" '
        f'stroke-linecap="round" stroke-linejoin="round">'
        f'<path d="{QMARK}"/></g>'
        f'<circle cx="512" cy="712" r="56" fill="{dot_fill}"/>'
    )


def variant_a1():
    """Compass question-mark, corporate blue gradient badge, yellow dot."""
    defs = _svg_defs([
        ("bg", (0, 0, 620, 1024), [(0, "#0A80CC"), (0.55, BLUE), (1, "#073A69")]),
    ])
    return f"""<svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
<title>ThinkBuddy — Compass / corporate blue</title>
{defs}
<path d="{BADGE}" fill="url(#bg)"/>
{qmark_group("#FFFFFF", YELLOW)}
</svg>"""


def variant_a2():
    """Compass question-mark, deep navy badge (closest to corporate deep blue)."""
    defs = _svg_defs([
        ("bg", (0, 0, 560, 1024), [(0, "#215AA6"), (0.5, NAVY), (1, "#0A1F5C")]),
    ])
    return f"""<svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
<title>ThinkBuddy — Compass / deep navy</title>
{defs}
<path d="{BADGE}" fill="url(#bg)"/>
{qmark_group("#FFFFFF", YELLOW)}
</svg>"""


def variant_b1():
    """Ribbon 't' (current live mark) recoloured to the corporate blue."""
    defs = _svg_defs([
        ("bg", (0, 0, 620, 1024), [(0, "#0A80CC"), (0.55, BLUE), (1, "#073A69")]),
        ("glyph", (0, 0, 1024, 1024), [(0, "#FFFFFF"), (1, "#EAF4FB")]),
    ])
    clip = f'<clipPath id="clip"><path d="{BADGE}"/></clipPath>'
    mask = """<mask id="m" maskUnits="userSpaceOnUse" x="0" y="0" width="1024" height="1024">
<rect width="1024" height="1024" fill="#000"/>
<g fill="#FFFFFF">
<path d="M 423 171 L 538 171 A 12 12 0 0 1 550 183 L 550 691 L 411 691 L 411 183 A 12 12 0 0 1 423 171 Z"/>
<path d="M 411 689 L 698 689 A 16 16 0 0 1 714 705 L 714 796 A 26 26 0 0 1 688 822 L 550 822 A 139 133 0 0 1 411 689 Z"/>
<path d="M 297 305 L 413 305 L 413 444 L 297 444 A 12 12 0 0 1 285 432 L 285 317 A 12 12 0 0 1 297 305 Z"/>
</g>
<circle cx="651" cy="403" r="76" fill="#000"/>
<path d="M 644.3 466.6 L 618 497 L 615.2 456.1 Z" fill="#000"/>
</mask>"""
    # the bubble dot is carved out of the glyph, so paint it back in yellow
    return f"""<svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
<title>ThinkBuddy — Ribbon t / corporate blue</title>
{defs}{clip}{mask}
<path d="{BADGE}" fill="url(#bg)"/>
<g clip-path="url(#clip)"><rect width="1024" height="1024" fill="url(#glyph)" mask="url(#m)"/></g>
<circle cx="651" cy="403" r="76" fill="{YELLOW}"/>
<path d="M 644.3 466.6 L 618 497 L 615.2 456.1 Z" fill="{YELLOW}"/>
</svg>"""


def variant_c1():
    """Talking: speech-bubble plus sign."""
    defs = _svg_defs([
        ("bg", (0, 0, 620, 1024), [(0, "#0A80CC"), (0.55, BLUE), (1, "#073A69")]),
    ])
    plus = (
        '<g stroke="#FFFFFF" stroke-width="112" stroke-linecap="round" fill="none">'
        '<path d="M 512 300 V 596"/><path d="M 364 448 H 660"/></g>'
        f'<circle cx="512" cy="742" r="56" fill="{YELLOW}"/>'
        '<path d="M 452 700 L 512 660 L 572 700 Z" fill="#FFFFFF"/>'
    )
    return f"""<svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
<title>ThinkBuddy — Talking plus / corporate blue</title>
{defs}
<path d="{BADGE}" fill="url(#bg)"/>
{plus}
</svg>"""


def variant_d1():
    """Transparent badge-free mark: corporate tri-colour gradient glyph."""
    defs = _svg_defs([
        ("g", (300, 240, 720, 800),
         [(0, BLUE), (0.52, SKY), (0.74, YELLOW), (1, ORANGE)]),
    ])
    return f"""<svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
<title>ThinkBuddy — transparent gradient mark</title>
{defs}
{qmark_group("url(#g)", "url(#g)")}
</svg>"""


VARIANTS = {
    "T1-compass-blue": variant_a1,
    "T2-compass-navy": variant_a2,
    "T3-ribbon-t-blue": variant_b1,
    "T4-talking-plus": variant_c1,
    "T5-mark-gradient": variant_d1,
}


def write_all(sizes=(1024, 512, 256, 128, 64, 48, 32, 16)):
    os.makedirs(HERE, exist_ok=True)
    made = []
    for name, fn in VARIANTS.items():
        svg = fn()
        svg_path = os.path.join(HERE, f"{name}.svg")
        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(svg)
        imgs = {}
        for s in sizes:
            png = resvg_py.svg_to_bytes(svg_string=svg, width=s, height=s)
            p = os.path.join(HERE, "png", f"{name}-{s}.png")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(png)
            imgs[s] = Image.open(io.BytesIO(png)).convert("RGBA")
        ico = os.path.join(HERE, f"{name}.ico")
        imgs[256].save(
            ico, format="ICO",
            sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
        )
        made.append(name)
        print("rendered", name)
    return made


if __name__ == "__main__":
    write_all()
