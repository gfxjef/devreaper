"""Builds DevReaper.ico: black square, centered circle split red (left) / green (right).
Run: python make_icon.py"""
from pathlib import Path

from PIL import Image, ImageDraw

RED = (239, 68, 68)
GREEN = (34, 197, 94)
BLACK = (10, 10, 10)
SIZES = [16, 24, 32, 48, 64, 128, 256]


def draw(size: int) -> Image.Image:
    scale = 4  # draw large and downsample for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.16), fill=BLACK)
    r = s * 0.30
    box = (s / 2 - r, s / 2 - r, s / 2 + r, s / 2 + r)
    d.pieslice(box, 90, 270, fill=RED)     # left half
    d.pieslice(box, 270, 90, fill=GREEN)   # right half
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    out = Path(__file__).with_name("DevReaper.ico")
    big = draw(256)
    big.save(out, sizes=[(n, n) for n in SIZES], append_images=[draw(n) for n in SIZES[:-1]])
    big.save(out.with_suffix(".png"))
    print(out)
