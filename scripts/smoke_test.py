"""Offline smoke test — exercises images + site + QR without any API key.

Builds a sample Menu, runs placeholder image generation, renders the site, and
generates the QR + table tent. Verifies the non-LLM pipeline works end to end.
(Extraction needs a vision-LLM key, so it's covered by the real run, not here.)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.modules.images.service import generate_image  # noqa: E402
from app.modules.qr.service import generate_qr, generate_table_tent  # noqa: E402
from app.modules.site.service import build_render_sections, render_menu_site  # noqa: E402
from app.schemas.menu import Menu, MenuItem, MenuSection  # noqa: E402

SAMPLE = Menu(
    restaurant_name="Smoke Test Cafe",
    currency="INR",
    sections=[
        MenuSection(
            name="Starters",
            items=[
                MenuItem(name="Paneer Tikka", description="Char-grilled cottage cheese",
                         price=249, is_vegetarian=True, spice_level="medium"),
                MenuItem(name="Chicken 65", description="Spicy fried chicken",
                         price=299, is_vegetarian=False, spice_level="spicy"),
            ],
        ),
        MenuSection(
            name="Beverages",
            items=[MenuItem(name="Masala Chai", price=40, is_vegetarian=True)],
        ),
    ],
)


def main() -> None:
    settings = get_settings()
    out_dir = Path("samples/output/_smoke")
    out_dir.mkdir(parents=True, exist_ok=True)

    image_rel: dict[str, str] = {}
    for s_idx, section in enumerate(SAMPLE.sections):
        for i_idx, item in enumerate(section.items):
            key = f"{s_idx}-{i_idx}"
            generate_image(item.name, item.description, settings, out_dir / "images" / f"{key}.png")
            image_rel[key] = f"images/{key}.png"

    sections = build_render_sections(SAMPLE, settings.symbol_for(SAMPLE.currency), image_rel)
    index_file = render_menu_site(
        restaurant_name=SAMPLE.restaurant_name, sections=sections, out_dir=out_dir,
        whatsapp="919876543210",
    )
    generate_qr("http://localhost:8000/smoke-test-cafe", out_dir / "qr.png")
    generate_table_tent("http://localhost:8000/smoke-test-cafe", SAMPLE.restaurant_name,
                        out_dir / "tent.png")

    expected = [index_file, out_dir / "qr.png", out_dir / "tent.png",
                out_dir / "images" / "0-0.png"]
    missing = [p for p in expected if not p.exists()]
    if missing:
        raise SystemExit(f"FAIL — missing outputs: {missing}")

    html = index_file.read_text(encoding="utf-8")
    assert "Paneer Tikka" in html and "₹249" in html and "Order on WhatsApp" in html, \
        "FAIL — rendered HTML missing expected content"

    print("PASS — images, site, and QR generated:")
    for p in expected:
        print(f"  {p}  ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
