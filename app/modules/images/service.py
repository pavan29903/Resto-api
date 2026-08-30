"""Dish images.

A real photograph of the dish beats an AI guess at it, so the default provider
searches Pexels — free, and explicitly licensed for commercial use, which
scraped web images are not. Stock libraries are thin on specific regional
dishes though ("Veg Manchurian" may return generic noodles), so this falls
through a chain rather than relying on any single source:

    pexels  ->  AI generation (pollinations)  ->  offline placeholder

Providers:
  - "pexels" (default): real licensed photographs, ~instant, free
    (200 req/hour, 20k/month). Needs PEXELS_API_KEY.
  - "pollinations": FLUX generation, free, no key, but ~15s per image.
  - "placeholder": a labelled card drawn with Pillow. Instant, offline.
  - "fal" / "replicate": paid FLUX, ~$0.003/image.

Whatever lands here, the owner can replace any image with a real photo of
their own dish — that stays the final say on accuracy.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.core.config import Settings

_WIDTH, _HEIGHT = 768, 512
_UA = "MenuSnap/0.1 (Phase 0 pipeline)"


def _prompt(name: str, description: str) -> str:
    base = f"{name}. {description}".strip().rstrip(".")
    return (
        f"{base}, professional food photography, appetizing, served on a plate, "
        "soft natural lighting, shallow depth of field, top-down"
    )


def _run_provider(
    provider: str, name: str, description: str, settings: Settings, out_path: Path
) -> Path:
    if provider == "pexels":
        return _pexels(name, description, settings, out_path)
    if provider == "pollinations":
        return _pollinations(name, description, settings, out_path)
    if provider == "fal":
        return _fal(name, description, settings, out_path)
    if provider == "replicate":
        return _replicate(name, description, settings, out_path)
    if provider == "placeholder":
        return _placeholder(name, out_path)
    raise ValueError(f"Unknown image provider '{provider}'")


def generate_image(name: str, description: str, settings: Settings, out_path: Path) -> Path:
    """Find or make one dish image, write it to out_path, and return that path.

    Walks the provider chain and never raises: a single dish that can't be
    illustrated must not break a 40-dish onboarding run.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Chain: the configured provider, then its fallback, then the offline card.
    chain = [settings.image_provider]
    if settings.image_fallback and settings.image_fallback != settings.image_provider:
        chain.append(settings.image_fallback)
    if "placeholder" not in chain:
        chain.append("placeholder")

    for provider in chain:
        try:
            return _run_provider(provider, name, description, settings, out_path)
        except _NoMatch:
            continue  # expected: stock search found nothing usable
        except Exception as exc:
            print(f"    ! {provider} failed for '{name}': {exc}")
            continue

    return _placeholder(name, out_path)


class _NoMatch(Exception):
    """A stock search ran fine but returned nothing usable for this dish."""


# Words that make a stock search worse rather than better.
_STOPWORDS = {
    "with", "and", "the", "in", "of", "a", "an", "our", "special", "house",
    "fresh", "homemade", "served", "style", "combo", "plate", "half", "full",
}


def _search_terms(name: str) -> list[str]:
    """Queries to try, most specific first.

    'Paneer Tikka Masala (Half)' -> ['paneer tikka masala food',
                                     'paneer tikka food', 'paneer food']
    """
    cleaned = re.sub(r"[\(\[].*?[\)\]]", " ", name)          # drop "(Half)"
    cleaned = re.sub(r"[^\w\s]", " ", cleaned).lower()
    words = [w for w in cleaned.split() if w and w not in _STOPWORDS and not w.isdigit()]
    if not words:
        return ["indian food dish"]

    queries = [" ".join(words[:3]) + " food"]
    if len(words) > 2:
        queries.append(" ".join(words[:2]) + " food")
    if len(words) > 1:
        queries.append(words[0] + " food")
    # De-duplicate, preserving order.
    return list(dict.fromkeys(queries))


def _pexels(name: str, description: str, settings: Settings, out_path: Path) -> Path:
    """Search Pexels for a real photograph of this dish.

    Pexels content is free for commercial use with no attribution required,
    which is what makes it safe to put on a paying restaurant's menu.
    """
    key = settings.pexels_api_key
    if not key:
        raise _NoMatch("no PEXELS_API_KEY set")

    for query in _search_terms(name):
        url = (
            "https://api.pexels.com/v1/search?"
            + urllib.parse.urlencode(
                {"query": query, "per_page": 1, "orientation": "landscape"}
            )
        )
        req = urllib.request.Request(
            url, headers={"Authorization": key, "User-Agent": _UA}
        )
        with urllib.request.urlopen(req, timeout=25) as resp:  # noqa: S310
            payload = json.load(resp)

        photos = payload.get("photos") or []
        if not photos:
            continue

        src = photos[0].get("src", {})
        # "large" is ~940px wide — plenty for a menu, and small enough to load
        # fast on a phone in a restaurant.
        image_url = src.get("large") or src.get("medium") or src.get("original")
        if not image_url:
            continue

        img_req = urllib.request.Request(image_url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(img_req, timeout=45) as img_resp:  # noqa: S310
            raw = img_resp.read()

        # Normalise to the same box every other provider writes, so the menu
        # layout doesn't shift depending on where a photo came from.
        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            im = ImageOps.fit(im, (_WIDTH, _HEIGHT), method=Image.LANCZOS)
            im.save(out_path, format="PNG")
        return out_path

    raise _NoMatch(f"no Pexels result for '{name}'")


def _pollinations(name: str, description: str, settings: Settings, out_path: Path) -> Path:
    """Free FLUX images via pollinations.ai — no API key, no signup.

    Anonymous access is rate-limited to roughly one request every 15 seconds,
    so we retry with backoff on 429 rather than giving up.
    """
    prompt = urllib.parse.quote(_prompt(name, description), safe="")
    url = (
        f"https://image.pollinations.ai/prompt/{prompt}"
        f"?width={_WIDTH}&height={_HEIGHT}"
        f"&model={settings.pollinations_model}&nologo=true"
    )

    last_error: Exception | None = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310
                out_path.write_bytes(resp.read())
            return out_path
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (429, 502, 503):  # rate-limited / transient
                time.sleep(16 * (attempt + 1))
                continue
            raise
        except Exception as exc:  # network blip
            last_error = exc
            time.sleep(5 * (attempt + 1))

    raise RuntimeError(f"pollinations failed after retries: {last_error}")


def _placeholder(name: str, out_path: Path) -> Path:
    # Deterministic, pleasant background colour derived from the dish name.
    digest = hashlib.md5(name.encode("utf-8")).digest()
    bg = (90 + digest[0] % 120, 90 + digest[1] % 120, 90 + digest[2] % 120)

    img = Image.new("RGB", (_WIDTH, _HEIGHT), bg)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=44)
        small = ImageFont.load_default(size=22)
    except TypeError:  # Pillow < 10.1 has no size arg
        font = ImageFont.load_default()
        small = font

    lines = textwrap.wrap(name, width=18) or [name]
    line_h = (draw.textbbox((0, 0), "Ag", font=font)[3]) + 10
    total_h = line_h * len(lines)
    y = (_HEIGHT - total_h) // 2
    for line in lines:
        w = draw.textbbox((0, 0), line, font=font)[2]
        draw.text(((_WIDTH - w) // 2, y), line, fill="white", font=font)
        y += line_h

    tag = "AI placeholder — replace with a real photo"
    tw = draw.textbbox((0, 0), tag, font=small)[2]
    draw.text(((_WIDTH - tw) // 2, _HEIGHT - 44), tag, fill=(255, 255, 255, 180), font=small)

    img.save(out_path)
    return out_path


def _download(url: str, out_path: Path) -> Path:
    with urllib.request.urlopen(url) as resp:  # noqa: S310 - trusted provider URL
        out_path.write_bytes(resp.read())
    return out_path


def _fal(name: str, description: str, settings: Settings, out_path: Path) -> Path:
    try:
        import os

        import fal_client
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "IMAGE_PROVIDER=fal but fal-client is not installed. Run: uv sync --extra fal"
        ) from exc

    if settings.fal_api_key:
        os.environ["FAL_KEY"] = settings.fal_api_key

    result = fal_client.subscribe(
        settings.image_model,
        arguments={"prompt": _prompt(name, description), "image_size": "landscape_4_3"},
        with_logs=False,
    )
    url = result["images"][0]["url"]
    return _download(url, out_path)


def _replicate(name: str, description: str, settings: Settings, out_path: Path) -> Path:
    try:
        import os

        import replicate
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "IMAGE_PROVIDER=replicate but replicate is not installed. "
            "Run: uv sync --extra replicate"
        ) from exc

    if settings.replicate_api_token:
        os.environ["REPLICATE_API_TOKEN"] = settings.replicate_api_token

    output = replicate.run(
        settings.image_model, input={"prompt": _prompt(name, description)}
    )
    item = output[0] if isinstance(output, (list, tuple)) else output
    # Newer replicate SDK returns FileOutput objects with .url / .read().
    if hasattr(item, "read"):
        out_path.write_bytes(item.read())
        return out_path
    url = item.url if hasattr(item, "url") else str(item)
    return _download(url, out_path)
