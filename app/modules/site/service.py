"""Static menu-site generation.

Renders a single self-contained index.html for a restaurant's menu — the
Phase 0 "nice template". In Phase 1 this becomes the Next.js diner menu app;
the render data shape here mirrors what that app will consume.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _format_price(price: float | None, symbol: str) -> str:
    if price is None:
        return ""
    # Drop the trailing .0 for whole-number prices (₹249, not ₹249.0).
    if float(price).is_integer():
        return f"{symbol}{int(price)}"
    return f"{symbol}{price:.2f}"


def build_render_sections(menu, symbol: str, image_paths: dict[str, str]) -> list[dict]:
    """Turn a Menu + generated image paths into template-friendly dicts.

    `image_paths` maps a stable item key ("<section_idx>-<item_idx>") to a
    relative image path (or is absent if no image was generated).
    """
    sections = []
    for s_idx, section in enumerate(menu.sections):
        items = []
        for i_idx, item in enumerate(section.items):
            key = f"{s_idx}-{i_idx}"
            items.append(
                {
                    "name": item.name,
                    "description": item.description,
                    "price": _format_price(item.price, symbol),
                    "is_vegetarian": item.is_vegetarian,
                    "spice_level": item.spice_level,
                    "image": image_paths.get(key),
                }
            )
        sections.append({"name": section.name, "items": items})
    return sections


def render_menu_site(
    *,
    restaurant_name: str,
    sections: list[dict],
    out_dir: Path,
    whatsapp: str | None = None,
) -> Path:
    """Render index.html into out_dir. Returns the path to the written file."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("menu.html.j2")
    html = template.render(
        restaurant_name=restaurant_name,
        sections=sections,
        whatsapp=whatsapp,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "index.html"
    out_file.write_text(html, encoding="utf-8")
    return out_file
