"""Generate Echo Bike Tracker .icns icon from scratch."""
import math, os, subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

def make_icon(size: int) -> Image.Image:
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad  = size * 0.06

    # ── Rounded-square background ──────────────────────────────────────────
    r    = size * 0.22
    bg   = [(pad, pad), (size - pad, size - pad)]
    draw.rounded_rectangle(bg, radius=r, fill=(13, 13, 26, 255))

    # ── Outer glow ring ────────────────────────────────────────────────────
    ring_w = max(1, size * 0.012)
    margin = pad + size * 0.045
    draw.ellipse(
        [(margin, margin), (size - margin, size - margin)],
        outline=(255, 107, 53, 60), width=int(ring_w)
    )

    # ── Power arc (270°) ──────────────────────────────────────────────────
    arc_m  = size * 0.12
    arc_bb = [(arc_m, arc_m), (size - arc_m, size - arc_m)]
    arc_w  = max(2, int(size * 0.055))
    # track
    draw.arc(arc_bb, start=135, end=45, fill=(30, 30, 60, 255), width=arc_w)
    # progress (~70% filled = good workout)
    end_angle = 135 + int(270 * 0.72)
    draw.arc(arc_bb, start=135, end=end_angle, fill=(255, 107, 53, 255), width=arc_w)

    # ── Lightning bolt (power symbol) ─────────────────────────────────────
    cx, cy = size / 2, size / 2
    s      = size * 0.22
    bolt   = [
        (cx + s*0.1,  cy - s*0.95),
        (cx - s*0.18, cy - s*0.05),
        (cx + s*0.08, cy - s*0.05),
        (cx - s*0.10, cy + s*0.95),
        (cx + s*0.18, cy + s*0.05),
        (cx - s*0.08, cy + s*0.05),
    ]
    draw.polygon(bolt, fill=(255, 107, 53, 255))

    # ── Subtle inner glow behind bolt ──────────────────────────────────────
    glow_r = int(size * 0.22)
    glow   = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd     = ImageDraw.Draw(glow)
    gd.ellipse(
        [(cx - glow_r, cy - glow_r), (cx + glow_r, cy + glow_r)],
        fill=(255, 107, 53, 35)
    )
    glow   = glow.filter(ImageFilter.GaussianBlur(size * 0.06))
    img    = Image.alpha_composite(img, glow)

    # re-draw bolt on top of glow
    draw   = ImageDraw.Draw(img)
    draw.polygon(bolt, fill=(255, 107, 53, 255))

    # small highlight on bolt tip
    tip_r = max(1, int(size * 0.025))
    tx, ty = cx + s*0.1, cy - s*0.95
    draw.ellipse([(tx-tip_r, ty-tip_r), (tx+tip_r, ty+tip_r)], fill=(255, 200, 150, 200))

    return img

def build_icns(out_dir: Path):
    iconset = out_dir / "EchoBike.iconset"
    iconset.mkdir(parents=True, exist_ok=True)

    specs = [
        (16,  "icon_16x16.png"),
        (32,  "icon_16x16@2x.png"),
        (32,  "icon_32x32.png"),
        (64,  "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024,"icon_512x512@2x.png"),
    ]
    for px, fname in specs:
        img = make_icon(px)
        img.save(iconset / fname)
        print(f"  {fname}  ({px}×{px})")

    icns = out_dir / "EchoBike.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
    print(f"\n✓  {icns}")
    return icns

if __name__ == "__main__":
    out = Path(__file__).parent / "assets"
    out.mkdir(exist_ok=True)
    build_icns(out)
