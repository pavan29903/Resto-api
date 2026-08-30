"""Menu extraction: photo(s) -> structured Menu via a vision LLM.

Default provider is Claude (Anthropic SDK, `messages.parse` with a Pydantic
schema so the result is validated and directly usable). Gemini is an optional
alternative. This module is the seed of the Phase 1 backend `extraction` module.
"""

from __future__ import annotations

import base64
from pathlib import Path

from app.core.config import Settings
from app.schemas.menu import Menu

MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

EXTRACTION_PROMPT = """You are given one or more photographs of a restaurant's physical menu card(s).
Extract the COMPLETE menu as structured data.

Rules:
- Capture every dish you can read, grouped under its printed section/category heading
  (e.g. "Starters", "Main Course", "Beverages"). If there are no headings, put everything
  in a single section named "Menu".
- For each dish: the exact name; a short description only if one is printed (else an empty
  string); and the price as a number only, with no currency symbol (e.g. 249.0). If a dish
  has no printed price, use null.
- Set is_vegetarian to true or false ONLY if the menu indicates it (veg/non-veg symbols or
  labels); otherwise null.
- Set spice_level to "mild", "medium", or "spicy" ONLY if indicated; otherwise null.
- Preserve the menu's original language for names and descriptions. Never invent items,
  prices, or descriptions — if you cannot read something, omit it.
- If multiple photos are provided, merge them into one menu without duplicating items.
- Detect the currency from symbols (₹ -> INR, $ -> USD, etc.); default to "INR" if unclear.
"""


def _media_type(path: Path) -> str:
    mt = MEDIA_TYPES.get(path.suffix.lower())
    if mt is None:
        raise ValueError(
            f"Unsupported image type '{path.suffix}' for {path.name}. "
            f"Use one of: {', '.join(sorted(MEDIA_TYPES))}"
        )
    return mt


def extract_menu(image_paths: list[Path], settings: Settings) -> Menu:
    """Extract a structured Menu from one or more menu-card photos."""
    if not image_paths:
        raise ValueError("No menu images provided.")
    if settings.extraction_provider == "gemini":
        return _extract_gemini(image_paths, settings)
    return _extract_claude(image_paths, settings)


def _extract_claude(image_paths: list[Path], settings: Settings) -> Menu:
    import anthropic

    # If no explicit key, let the SDK resolve credentials (env var / `ant auth login`).
    client = (
        anthropic.Anthropic(api_key=settings.anthropic_api_key)
        if settings.anthropic_api_key
        else anthropic.Anthropic()
    )

    content: list[dict] = []
    for path in image_paths:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _media_type(path),
                    "data": base64.standard_b64encode(path.read_bytes()).decode("utf-8"),
                },
            }
        )
    content.append({"type": "text", "text": EXTRACTION_PROMPT})

    response = client.messages.parse(
        model=settings.extraction_model,
        max_tokens=16000,
        messages=[{"role": "user", "content": content}],
        output_format=Menu,
    )

    menu = response.parsed_output
    if menu is None:
        raise RuntimeError(
            f"Extraction produced no structured output (stop_reason={response.stop_reason}). "
            "Try a clearer photo, or a stronger EXTRACTION_MODEL."
        )
    return menu


def _extract_gemini(image_paths: list[Path], settings: Settings) -> Menu:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "Gemini provider selected but google-genai is not installed. "
            "Run: uv sync --extra gemini"
        ) from exc

    client = genai.Client(api_key=settings.google_api_key)
    parts: list = [
        types.Part.from_bytes(data=path.read_bytes(), mime_type=_media_type(path))
        for path in image_paths
    ]
    parts.append(types.Part.from_text(text=EXTRACTION_PROMPT))

    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=parts,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Menu,
        ),
    )
    menu = response.parsed
    if menu is None:
        raise RuntimeError("Gemini extraction returned no parseable structured output.")
    return menu
