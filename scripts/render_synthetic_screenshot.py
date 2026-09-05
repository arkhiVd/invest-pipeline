#!/usr/bin/env python3
"""Render a repository-safe dashboard mockup from fictional values."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "docs/screenshots/synthetic-dashboard.png"
BG = "#0e1117"
PANEL = "#1b2330"
TEXT = "#f3f4f6"
MUTED = "#9ca3af"
GREEN = "#34d399"


def font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(name, size)


def main() -> None:
    image = Image.new("RGB", (1440, 900), BG)
    draw = ImageDraw.Draw(image)
    draw.text((55, 38), "invest-pipeline synthetic dashboard", fill=TEXT, font=font(34, True))
    draw.text(
        (55, 86),
        "Fictional instruments and values. Not financial advice.",
        fill=MUTED,
        font=font(18),
    )

    cards = [
        (55, 135, "Screen survivors", "3"),
        (385, 135, "New crossovers", "1"),
        (715, 135, "Data status", "CURRENT"),
        (1045, 135, "Broker mode", "OFFLINE"),
    ]
    for x, y, label, value in cards:
        draw.rounded_rectangle((x, y, x + 285, y + 125), 12, fill=PANEL)
        draw.text((x + 22, y + 20), label, fill=MUTED, font=font(17))
        draw.text((x + 22, y + 57), value, fill=GREEN, font=font(29, True))

    draw.rounded_rectangle((55, 300, 1385, 835), 12, fill=PANEL)
    headers = ["Symbol", "Screen", "Score", "EMA state", "Evidence"]
    xs = [90, 300, 610, 820, 1100]
    for x, label in zip(xs, headers, strict=True):
        draw.text((x, 335), label, fill=MUTED, font=font(18, True))
    rows = [
        ("ACME", "quality pullback", "82.5", "crossed above", "complete"),
        ("NOVA", "GARP", "76.0", "below", "complete"),
        ("ZEAL", "deep value", "unavailable", "equal", "missing ROCE"),
    ]
    for index, row in enumerate(rows):
        y = 400 + index * 105
        draw.line((85, y - 25, 1350, y - 25), fill="#344155", width=1)
        for x, value in zip(xs, row, strict=True):
            draw.text((x, y), value, fill=TEXT, font=font(18))
    draw.text(
        (90, 760),
        "Signals are research output, not orders or recommendations.",
        fill=MUTED,
        font=font(17),
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT, optimize=True)
    print(OUT)


if __name__ == "__main__":
    main()
