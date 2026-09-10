"""Menu extraction: photo(s) -> structured Menu via a vision LLM.

Default provider is Claude (Anthropic SDK, `messages.parse` with a Pydantic
schema so the result is validated and directly usable). Gemini is an optional
alternative. This module is the seed of the Phase 1 backend `extraction` module.
"""

from __future__ import annotations

import base64
import logging
import random
import time
from pathlib import Path

from app.core.config import Settings
from app.schemas.menu import Menu

logger = logging.getLogger(__name__)


class UnsupportedImage(ValueError):
    """The file itself is wrong. No provider will do better, so don't try."""


class ProviderBusy(RuntimeError):
    """The vision provider is temporarily overloaded.

    Distinct from a genuine failure because the caller should answer 503 and
    invite a retry, not 500. Reading a menu is the one step an owner cannot
    route around, so "try again in a moment" and "this is broken" must not
    look the same to them.
    """


# Matched against the exception text rather than a status attribute, because
# each provider SDK raises its own error type with its own shape. Crude, but
# the failure mode is safe: an unrecognised error is treated as permanent and
# surfaces immediately instead of being retried pointlessly.
_TRANSIENT_MARKERS = (
    "503",
    "429",
    "unavailable",
    "overloaded",
    "high demand",
    "resource_exhausted",
    "rate limit",
    "timeout",
    "deadline",
)

_ATTEMPTS = 3
_BACKOFF_SECONDS = (2.0, 5.0)


def _is_transient(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)

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
        raise UnsupportedImage(
            f"Unsupported image type '{path.suffix}' for {path.name}. "
            f"Use one of: {', '.join(sorted(MEDIA_TYPES))}"
        )
    return mt


def extract_menu(image_paths: list[Path], settings: Settings) -> Menu:
    """Extract a structured Menu from one or more menu-card photos.

    Retries a busy provider before giving up. Free vision tiers return 503
    under load often enough that a single attempt makes the product look
    unreliable, and these overloads usually clear within seconds.

    Blocking sleeps are fine here: the caller runs this in a worker thread, so
    the event loop keeps serving other requests while we wait.
    """
    if not image_paths:
        raise ValueError("No menu images provided.")

    chain = settings.extraction_chain
    if not chain:
        raise RuntimeError(
            f"No API key configured for extraction provider "
            f"'{settings.extraction_provider}'."
        )

    last: Exception | None = None
    for index, (provider, model) in enumerate(chain):
        try:
            menu = _try_provider(provider, model, image_paths, settings)
        except UnsupportedImage:
            # The owner's file is the problem. Every provider would reject it,
            # and telling them so immediately beats a slow tour of all three.
            raise
        except Exception as exc:  # noqa: BLE001 — the next provider is the handler
            last = exc
            remaining = len(chain) - index - 1
            logger.warning(
                "Extraction via %s/%s failed (%s more to try): %s",
                provider,
                model,
                remaining,
                exc,
            )
            continue

        if index:
            logger.info("Extraction succeeded on fallback %s/%s", provider, model)
        return menu

    # Every provider was busy rather than broken, so this is worth retrying.
    if last is not None and _is_transient(last):
        raise ProviderBusy(f"All {len(chain)} vision providers busy") from last
    raise RuntimeError(f"All {len(chain)} vision providers failed") from last


def _try_provider(
    provider: str, model: str, image_paths: list[Path], settings: Settings
) -> Menu:
    """One provider, retried while it reports a transient fault."""
    run = {
        "gemini": _extract_gemini,
        "claude": _extract_claude,
        "openai": _extract_openai,
    }.get(provider)
    if run is None:
        raise RuntimeError(f"Unknown extraction provider '{provider}'.")

    for attempt in range(_ATTEMPTS):
        try:
            return run(image_paths, settings, model)
        except Exception as exc:
            # A bad key or an unreadable photo fails identically every time —
            # retrying only delays the same answer. Hand it to the next
            # provider instead, which may well have a valid key.
            if not _is_transient(exc) or attempt == _ATTEMPTS - 1:
                raise
            # Jitter so simultaneous uploads don't retry in lockstep.
            delay = _BACKOFF_SECONDS[attempt] + random.uniform(0, 0.5)
            logger.warning(
                "%s/%s busy (attempt %d/%d), retrying in %.1fs",
                provider,
                model,
                attempt + 1,
                _ATTEMPTS,
                delay,
            )
            time.sleep(delay)

    raise AssertionError("unreachable")  # pragma: no cover


def _extract_claude(image_paths: list[Path], settings: Settings, model: str) -> Menu:
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
        model=model,
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


def _extract_gemini(image_paths: list[Path], settings: Settings, model: str) -> Menu:
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
        model=model,
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


def _extract_openai(image_paths: list[Path], settings: Settings, model: str) -> Menu:
    """OpenAI vision with structured outputs.

    Kept as a fallback rather than the default because it is the only provider
    here with no free tier — it stays dormant until OPENAI_API_KEY is set.
    """
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "OpenAI provider selected but the openai package is not installed. "
            "Run: uv sync --extra openai"
        ) from exc

    client = OpenAI(api_key=settings.openai_api_key)

    content: list[dict] = [
        {
            "type": "image_url",
            "image_url": {
                "url": (
                    f"data:{_media_type(path)};base64,"
                    f"{base64.standard_b64encode(path.read_bytes()).decode('utf-8')}"
                )
            },
        }
        for path in image_paths
    ]
    content.append({"type": "text", "text": EXTRACTION_PROMPT})

    response = client.beta.chat.completions.parse(
        model=model,
        messages=[{"role": "user", "content": content}],
        response_format=Menu,
    )

    menu = response.choices[0].message.parsed
    if menu is None:
        refusal = response.choices[0].message.refusal
        raise RuntimeError(
            f"OpenAI extraction returned no structured output"
            + (f" (refused: {refusal})" if refusal else ".")
        )
    return menu
