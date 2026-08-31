"""Phase 0 pipeline: menu photo(s) -> structured menu -> dish images -> site + QR.

This is the concierge-MVP runner from the plan. It is deliberately a plain
script (no DB, no web server) but it imports the real `app.modules.*` code, so
the extraction/images/site/qr logic it exercises is what the Phase 1 FastAPI
backend will reuse.

Usage:
    uv run python scripts/phase0.py --images samples/menu.jpg --name "Blue Tokai"
    uv run python scripts/phase0.py --images samples/ --name "Cafe X" --menu-url https://host/cafe-x
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the project root importable when run as `python scripts/phase0.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slugify import slugify  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.modules.extraction.service import MEDIA_TYPES, extract_menu  # noqa: E402
from app.modules.images.service import generate_image  # noqa: E402
from app.modules.qr.service import generate_qr, generate_table_tent  # noqa: E402
from app.modules.site.service import build_render_sections, render_menu_site  # noqa: E402


def _collect_images(paths: list[str]) -> list[Path]:
    """Expand file/dir args into a sorted list of image paths."""
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found.extend(
                child
                for child in sorted(p.iterdir())
                if child.suffix.lower() in MEDIA_TYPES
            )
        elif p.is_file():
            found.append(p)
        else:
            raise SystemExit(f"Not found: {raw}")
    if not found:
        raise SystemExit("No menu images found. Point --images at a photo or a folder.")
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="RestoFood Phase 0 pipeline")
    parser.add_argument(
        "--images", nargs="+", required=True, help="Menu photo(s) or a folder of photos."
    )
    parser.add_argument("--name", help="Restaurant name (overrides what the menu shows).")
    parser.add_argument("--slug", help="URL slug (default: derived from the name).")
    parser.add_argument("--out", help="Output folder (default: samples/output/<slug>).")
    parser.add_argument(
        "--menu-url",
        help="Public URL the QR should point at (default: PUBLIC_BASE_URL/<slug>).",
    )
    parser.add_argument("--skip-images", action="store_true", help="Skip dish-image generation.")
    parser.add_argument("--skip-qr", action="store_true", help="Skip QR / table-tent generation.")

    # Model overrides — try the free models first, escalate without editing .env.
    parser.add_argument(
        "--provider",
        choices=["gemini", "claude"],
        help="Extraction provider (default from .env: gemini).",
    )
    parser.add_argument(
        "--model",
        help="Extraction model id, e.g. gemini-2.5-flash / claude-opus-4-8.",
    )
    parser.add_argument(
        "--image-provider",
        choices=["pollinations", "placeholder", "fal", "replicate"],
        help="Image provider (default from .env: pollinations).",
    )
    args = parser.parse_args()

    settings = get_settings()

    # Apply CLI overrides on top of .env.
    overrides: dict = {}
    if args.provider:
        overrides["extraction_provider"] = args.provider
    if args.model:
        key = "gemini_model" if (args.provider or settings.extraction_provider) == "gemini" \
            else "extraction_model"
        overrides[key] = args.model
    if args.image_provider:
        overrides["image_provider"] = args.image_provider
    if overrides:
        settings = settings.model_copy(update=overrides)

    image_paths = _collect_images(args.images)

    active_model = (
        settings.gemini_model
        if settings.extraction_provider == "gemini"
        else settings.extraction_model
    )
    print(f"→ Extracting menu from {len(image_paths)} photo(s) "
          f"via {settings.extraction_provider} ({active_model})…")
    menu = extract_menu(image_paths, settings)

    restaurant_name = args.name or menu.restaurant_name or "My Restaurant"
    slug = args.slug or slugify(restaurant_name)
    out_dir = Path(args.out) if args.out else Path("samples/output") / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    item_count = sum(len(s.items) for s in menu.sections)
    print(f"  ✓ {len(menu.sections)} sections, {item_count} items "
          f"(currency: {menu.currency})")

    # Persist the structured menu — this is the owner's review/edit artifact.
    (out_dir / "menu.json").write_text(
        json.dumps(menu.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  ✓ wrote {out_dir / 'menu.json'}")

    # Generate one image per dish (unless skipped).
    image_rel_paths: dict[str, str] = {}
    if not args.skip_images:
        print(f"→ Generating {item_count} dish image(s) via {settings.image_provider}…")
        img_dir = out_dir / "images"
        for s_idx, section in enumerate(menu.sections):
            for i_idx, item in enumerate(section.items):
                key = f"{s_idx}-{i_idx}"
                out_path = img_dir / f"{key}.png"
                try:
                    generate_image(item.name, item.description, settings, out_path)
                    image_rel_paths[key] = f"images/{key}.png"
                except Exception as exc:  # keep going; a missing image isn't fatal
                    print(f"  ! image failed for '{item.name}': {exc}")
        print(f"  ✓ {len(image_rel_paths)} image(s) in {img_dir}")

    # Render the menu site.
    symbol = settings.symbol_for(menu.currency)
    sections = build_render_sections(menu, symbol, image_rel_paths)
    index_file = render_menu_site(
        restaurant_name=restaurant_name,
        sections=sections,
        out_dir=out_dir,
        whatsapp=settings.owner_whatsapp,
    )
    print(f"  ✓ wrote {index_file}")

    # Generate the QR + printable table tent.
    menu_url = args.menu_url or f"{settings.public_base_url.rstrip('/')}/{slug}"
    if not args.skip_qr:
        generate_qr(menu_url, out_dir / "qr.png")
        generate_table_tent(menu_url, restaurant_name, out_dir / "tent.png")
        print(f"  ✓ QR -> {menu_url}  ({out_dir / 'qr.png'}, {out_dir / 'tent.png'})")

    print("\nDone. Next steps:")
    print(f"  1. Review/fix the menu:  {out_dir / 'menu.json'}")
    print(f"  2. Preview locally:      open {index_file}")
    print(f"  3. Serve for phone test: cd {out_dir} && python -m http.server 8000")
    print("     then set --menu-url to http://<your-LAN-ip>:8000 and re-run "
          "with --skip-images to regenerate the QR.")
    print(f"  4. Print the table tent: {out_dir / 'tent.png'}")


if __name__ == "__main__":
    main()
