"""Generate Pinterest-spec pin images for today's YouTube videos.

Pinterest pins:
  - 1000 x 1500 (2:3 vertical) per Pinterest's algorithm preference
  - High contrast, bold headline (must read at thumbnail size)
  - Niche-appropriate color palette
  - Brand mark + URL slug at the bottom

Output: pinterest_pack/<date>/<niche>_<slug>.png

Run:
  python tools/generate_pinterest_pins.py
  python tools/generate_pinterest_pins.py 20260510   # specific date
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR_BASE = ROOT / "pinterest_pack"

PIN_W, PIN_H = 1000, 1500


# Each entry: niche -> (palette, font_color, accent, headline, kicker, slug, video_id)
# Palette: (top, bottom) gradient.
# Font color: title color.
# Accent: highlight strip color.
PINS = [
    {
        "niche": "tech-ai",
        "headline": "5 AI Jobs\nDISAPPEARING\nby 2027",
        "kicker": "AND THE 4 TAKING THEIR PLACE",
        "footer": "FULL BREAKDOWN ON YOUTUBE",
        "url": "youtube.com/@MatrixMonster",
        "video_id": "j8Bz8KWc4gE",
        "palette_top": (10, 18, 40),       # deep navy
        "palette_bottom": (40, 8, 80),     # purple
        "title_color": (255, 255, 255),
        "accent": (0, 230, 200),           # cyan
        "kicker_color": (255, 220, 100),   # warm yellow
    },
    {
        "niche": "finance",
        "headline": "The Roth IRA\nHack 85%\nof Investors\nMISS",
        "kicker": "+$10,000 BY 2026 (LEGAL)",
        "footer": "NOT FINANCIAL ADVICE",
        "url": "youtube.com/@MatrixMonster",
        "video_id": "qt9Vo-EA-eg",
        "palette_top": (4, 60, 35),        # dark green
        "palette_bottom": (8, 20, 14),     # near black green
        "title_color": (255, 255, 255),
        "accent": (255, 200, 60),          # gold
        "kicker_color": (255, 200, 60),
    },
    {
        "niche": "motivation",
        "headline": "STOP\nWASTING\n$15K on\nMOTIVATION\nTOOLS",
        "kicker": "THE ONE SYSTEM THAT REPLACED ALL OF THEM",
        "footer": "WATCH ON YOUTUBE",
        "url": "youtube.com/@MatrixMonster",
        "video_id": "nZy5iH7IuRc",
        "palette_top": (0, 0, 0),
        "palette_bottom": (60, 12, 12),    # blood red
        "title_color": (255, 255, 255),
        "accent": (255, 75, 50),           # bright red
        "kicker_color": (255, 220, 220),
    },
    {
        "niche": "history-mystery",
        "headline": "The\nArtifact\nThat\nSHOULDN'T\nEXIST",
        "kicker": "WHAT THE CARBON-DATING ACTUALLY SHOWED",
        "footer": "HIDDEN HISTORY ON YOUTUBE",
        "url": "youtube.com/@MatrixMonster",
        "video_id": "CbF42IySQTc",
        "palette_top": (45, 28, 12),       # vintage brown
        "palette_bottom": (15, 8, 4),      # dark brown/black
        "title_color": (245, 220, 175),    # parchment
        "accent": (200, 130, 60),          # bronze
        "kicker_color": (245, 220, 175),
    },
    # ---- SHORTS PINS (one per recovered Short) -----------------------------
    {
        "niche": "tech-ai-short",
        "headline": "AI JOBS\n90%\nGONE\nBY 2027",
        "kicker": "5 ROLES DISAPPEARING — 4 SURVIVING",
        "footer": "WATCH THE 60-SEC BREAKDOWN",
        "url": "youtu.be/mHaxX51eHDM",
        "video_id": "mHaxX51eHDM",
        "palette_top": (10, 18, 40),
        "palette_bottom": (40, 8, 80),
        "title_color": (255, 255, 255),
        "accent": (0, 230, 200),
        "kicker_color": (255, 220, 100),
    },
    {
        "niche": "tech-ai-short",
        "headline": "$15K/Q\nLOST TO\nONE AI\nMISTAKE",
        "kicker": "MOST STARTUPS DON'T CATCH THIS",
        "footer": "THE FIX, IN 60 SECONDS",
        "url": "youtu.be/ZsA2nj_NVNY",
        "video_id": "ZsA2nj_NVNY",
        "palette_top": (10, 18, 40),
        "palette_bottom": (40, 8, 80),
        "title_color": (255, 255, 255),
        "accent": (0, 230, 200),
        "kicker_color": (255, 220, 100),
    },
    {
        "niche": "tech-ai-short",
        "headline": "ML IS\nDEAD.\nLONG\nLIVE...?",
        "kicker": "THE SKILL THAT REPLACED IT",
        "footer": "60-SEC HOT TAKE",
        "url": "youtu.be/yX6A2LujmNk",
        "video_id": "yX6A2LujmNk",
        "palette_top": (10, 18, 40),
        "palette_bottom": (40, 8, 80),
        "title_color": (255, 255, 255),
        "accent": (0, 230, 200),
        "kicker_color": (255, 220, 100),
    },
    {
        "niche": "finance-short",
        "headline": "85%\nDON'T\nKNOW\nTHIS\nROTH\nHACK",
        "kicker": "+RETURNS, ZERO IRS RISK",
        "footer": "NOT FINANCIAL ADVICE",
        "url": "youtu.be/Ym6NyrLnd7Y",
        "video_id": "Ym6NyrLnd7Y",
        "palette_top": (4, 60, 35),
        "palette_bottom": (8, 20, 14),
        "title_color": (255, 255, 255),
        "accent": (255, 200, 60),
        "kicker_color": (255, 200, 60),
    },
    {
        "niche": "finance-short",
        "headline": "+$10K\nBY 2026\nROTH\nLOOPHOLE",
        "kicker": "STARTS BEFORE APRIL OR NEVER",
        "footer": "NOT FINANCIAL ADVICE",
        "url": "youtu.be/0_h7iravS4w",
        "video_id": "0_h7iravS4w",
        "palette_top": (4, 60, 35),
        "palette_bottom": (8, 20, 14),
        "title_color": (255, 255, 255),
        "accent": (255, 200, 60),
        "kicker_color": (255, 200, 60),
    },
    {
        "niche": "motivation-short",
        "headline": "$15K\nWASTED\nON\n\"MOTIVATION\"\nTOOLS",
        "kicker": "THE ONE SYSTEM THAT REPLACED ALL",
        "footer": "60-SEC TAKEDOWN",
        "url": "youtu.be/J0c65RQyzJA",
        "video_id": "J0c65RQyzJA",
        "palette_top": (0, 0, 0),
        "palette_bottom": (60, 12, 12),
        "title_color": (255, 255, 255),
        "accent": (255, 75, 50),
        "kicker_color": (255, 220, 220),
    },
    {
        "niche": "motivation-short",
        "headline": "THE\n#1\nTHING\nHOLDING\nYOU\nBACK",
        "kicker": "(IT'S NOT WHAT YOU THINK)",
        "footer": "60-SECOND ANSWER",
        "url": "youtu.be/vzTJjAzRLC0",
        "video_id": "vzTJjAzRLC0",
        "palette_top": (0, 0, 0),
        "palette_bottom": (60, 12, 12),
        "title_color": (255, 255, 255),
        "accent": (255, 75, 50),
        "kicker_color": (255, 220, 220),
    },
]


def find_font(*names_and_paths) -> Path | None:
    """Return the first existing font path."""
    candidates = [
        # Common Windows system fonts
        Path(r"C:\Windows\Fonts\impact.ttf"),
        Path(r"C:\Windows\Fonts\ariblk.ttf"),
        Path(r"C:\Windows\Fonts\arialbd.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\bahnschrift.ttf"),
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
        Path(r"C:\Windows\Fonts\seguisb.ttf"),
        Path(r"C:\Windows\Fonts\georgia.ttf"),
        Path(r"C:\Windows\Fonts\georgiab.ttf"),
    ] + [Path(p) for p in names_and_paths]
    for p in candidates:
        if p.exists():
            return p
    return None


def gradient(w: int, h: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    """Vertical linear gradient."""
    base = Image.new("RGB", (w, h), top)
    px = base.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(w):
            px[x, y] = (r, g, b)
    return base


def draw_outlined(draw: ImageDraw.ImageDraw, xy, text, font, fill, stroke_width=4, stroke_fill=(0, 0, 0)):
    draw.text(xy, text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)


def fit_headline_size(draw: ImageDraw.ImageDraw, text: str, font_path: Path, max_w: int, start_size: int) -> tuple[ImageFont.FreeTypeFont, int]:
    """Find the largest font size where the widest line of `text` fits in max_w."""
    size = start_size
    while size > 30:
        font = ImageFont.truetype(str(font_path), size)
        widest = max(
            draw.textbbox((0, 0), line, font=font)[2] - draw.textbbox((0, 0), line, font=font)[0]
            for line in text.split("\n")
        )
        if widest <= max_w:
            return font, size
        size -= 6
    return ImageFont.truetype(str(font_path), 30), 30


def render_pin(spec: dict, out_path: Path) -> None:
    headline_font_path = find_font()
    body_font_path = find_font(r"C:\Windows\Fonts\arial.ttf") or headline_font_path
    if headline_font_path is None:
        raise RuntimeError("No system fonts found — cannot render Pinterest pins")

    img = gradient(PIN_W, PIN_H, spec["palette_top"], spec["palette_bottom"])
    draw = ImageDraw.Draw(img, "RGBA")

    # Subtle radial glow at top (paint a brighter ring)
    glow = Image.new("RGBA", (PIN_W, PIN_H), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    cx, cy = PIN_W // 2, 220
    for r in range(360, 80, -20):
        alpha = max(0, 60 - (360 - r) // 4)
        glow_draw.ellipse(
            (cx - r, cy - r, cx + r, cy + r),
            fill=spec["accent"] + (alpha,),
        )
    img.paste(glow, (0, 0), glow)

    # Top accent strip
    draw.rectangle((0, 80, PIN_W, 96), fill=spec["accent"])

    # Kicker (top, all caps, accent color)
    kicker_size = 38
    kicker_font = ImageFont.truetype(str(body_font_path), kicker_size)
    kicker_text = spec["kicker"]
    kw = draw.textbbox((0, 0), kicker_text, font=kicker_font)[2]
    while kw > PIN_W - 120 and kicker_size > 22:
        kicker_size -= 2
        kicker_font = ImageFont.truetype(str(body_font_path), kicker_size)
        kw = draw.textbbox((0, 0), kicker_text, font=kicker_font)[2]
    draw.text(
        ((PIN_W - kw) // 2, 130),
        kicker_text,
        font=kicker_font,
        fill=spec["kicker_color"],
    )

    # Headline — fit-to-width, multi-line
    headline = spec["headline"]
    head_font, head_size = fit_headline_size(
        draw, headline, headline_font_path, max_w=PIN_W - 100, start_size=170
    )
    line_h = int(head_size * 1.05)
    n_lines = len(headline.split("\n"))
    total_h = n_lines * line_h
    y = (PIN_H - total_h) // 2
    for line in headline.split("\n"):
        bbox = draw.textbbox((0, 0), line, font=head_font)
        lw = bbox[2] - bbox[0]
        x = (PIN_W - lw) // 2
        draw_outlined(draw, (x, y), line, head_font, fill=spec["title_color"], stroke_width=6)
        y += line_h

    # Bottom call-to-action band
    band_h = 200
    draw.rectangle((0, PIN_H - band_h, PIN_W, PIN_H), fill=(0, 0, 0))
    draw.rectangle((0, PIN_H - band_h, PIN_W, PIN_H - band_h + 6), fill=spec["accent"])

    # Footer text
    footer_font = ImageFont.truetype(str(body_font_path), 42)
    footer_text = spec["footer"]
    fw = draw.textbbox((0, 0), footer_text, font=footer_font)[2]
    draw.text(
        ((PIN_W - fw) // 2, PIN_H - band_h + 38),
        footer_text,
        font=footer_font,
        fill=spec["accent"],
    )

    # URL
    url_font = ImageFont.truetype(str(body_font_path), 30)
    url_text = spec["url"]
    uw = draw.textbbox((0, 0), url_text, font=url_font)[2]
    draw.text(
        ((PIN_W - uw) // 2, PIN_H - band_h + 105),
        url_text,
        font=url_font,
        fill=(220, 220, 220),
    )

    # Tiny video id watermark for traceability
    wm_font = ImageFont.truetype(str(body_font_path), 18)
    draw.text(
        (PIN_W - 200, PIN_H - 30),
        f"id:{spec['video_id']}",
        font=wm_font,
        fill=(120, 120, 120),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)


def main() -> int:
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m%d")
    out_dir = OUT_DIR_BASE / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Generating {len(PINS)} Pinterest pins -> {out_dir}\n")
    for spec in PINS:
        slug = spec["niche"]
        out_path = out_dir / f"{slug}_{spec['video_id']}.png"
        render_pin(spec, out_path)
        size_kb = out_path.stat().st_size / 1024
        print(f"  {out_path.name}  ({PIN_W}x{PIN_H}, {size_kb:.0f} KB)")

    print(
        "\nDone. To post: open Pinterest, click '+', upload each pin, "
        "set destination URL to https://youtu.be/<video_id>, add 5-10 niche hashtags."
    )
    print(
        "Pinterest CTR floor for new pinners is ~0.3-0.5%. Affiliate-tagged pins on "
        "finance/motivation niches have historically converted 2-4x faster than X."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
