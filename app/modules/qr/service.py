"""QR code + printable table-tent generation.

Seed of the Phase 1 backend `qr` module (which will serve these as endpoints).
"""

from __future__ import annotations

from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont


def generate_qr(url: str, out_path: Path, box_size: int = 12) -> Path:
    """Write a plain QR PNG pointing at `url`."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(out_path)
    return out_path


def generate_table_tent(url: str, restaurant_name: str, out_path: Path) -> Path:
    """Write a printable A6-ish 'scan to view the menu' card with the QR centred."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 1050, 1480  # ~ A6 at 300 dpi
    card = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(card)

    try:
        title_font = ImageFont.load_default(size=64)
        sub_font = ImageFont.load_default(size=40)
        foot_font = ImageFont.load_default(size=30)
    except TypeError:  # Pillow < 10.1
        title_font = sub_font = foot_font = ImageFont.load_default()

    def centered(text: str, y: int, font) -> None:
        w = draw.textbbox((0, 0), text, font=font)[2]
        draw.text(((width - w) // 2, y), text, fill="black", font=font)

    centered(restaurant_name or "Our Menu", 120, title_font)
    centered("Scan to view the menu & order", 220, sub_font)

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=14, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_size = 760
    qr_img = qr_img.resize((qr_size, qr_size))
    card.paste(qr_img, ((width - qr_size) // 2, 340))

    centered("Point your phone camera at the code", height - 180, foot_font)

    card.save(out_path)
    return out_path
