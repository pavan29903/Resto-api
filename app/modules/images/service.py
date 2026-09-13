"""Dish images.

A real photograph of the dish beats an AI guess at it, so the default provider
searches Pexels — free, and explicitly licensed for commercial use, which
scraped web images are not. Stock libraries are thin on specific regional
dishes though ("Veg Manchurian" may return generic noodles), so this falls
through a chain rather than relying on any single source:

    pexels  ->  AI generation (pollinations)  ->  offline placeholder

Stock results are not taken on trust. Several candidates are fetched and shown
to a vision model, which picks the one that actually depicts the dish or
rejects them all — a wrong photograph on a restaurant's menu is worse than
none, and "Filter Coffee" returning a pour-over brewing kit is the kind of
thing a keyword search cannot catch. A rejection simply moves to the next
provider in the chain.

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

import base64
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
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.modules.extraction.service import _is_transient


class _Pick(BaseModel):
    """The vision model's verdict on a set of candidate photographs."""

    best_index: int = Field(
        description="1-based number of the best photograph, or 0 if none fit."
    )
    reason: str = Field(description="One short sentence explaining the choice.")

_WIDTH, _HEIGHT = 768, 512
_UA = "RestoFood/0.1 (Phase 0 pipeline)"

# Kept short: this runs once per dish, so a long backoff on a 40-dish menu
# would add minutes to a publish for a check that is only an improvement.
_CHECK_ATTEMPTS = 3
_CHECK_BACKOFF = 2.0


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


def _fetch(url: str, timeout: int = 45) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def _pexels(name: str, description: str, settings: Settings, out_path: Path) -> Path:
    """Search Pexels for a real photograph of this dish.

    Pexels content is free for commercial use with no attribution required,
    which is what makes it safe to put on a paying restaurant's menu.

    Several candidates are fetched rather than one, because the top stock hit
    for a regional Indian dish is frequently something else entirely. Which one
    (if any) actually depicts the dish is decided by `_pick_best`.
    """
    key = settings.pexels_api_key
    if not key:
        raise _NoMatch("no PEXELS_API_KEY set")

    wanted = max(1, settings.image_candidates) if settings.validate_images else 1

    for query in _search_terms(name):
        url = (
            "https://api.pexels.com/v1/search?"
            + urllib.parse.urlencode(
                {"query": query, "per_page": wanted, "orientation": "landscape"}
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

        chosen = _choose(photos, name, description, settings)
        if chosen is None:
            # Nothing here depicts the dish. A broader query would only be
            # vaguer, so stop searching Pexels and let the chain move on to
            # generating an image instead.
            raise _NoMatch(f"no Pexels photo matched '{name}'")

        src = chosen.get("src", {})
        # "large" is ~940px wide — plenty for a menu, and small enough to load
        # fast on a phone in a restaurant.
        image_url = src.get("large") or src.get("medium") or src.get("original")
        if not image_url:
            continue

        raw = _fetch(image_url)

        # Normalise to the same box every other provider writes, so the menu
        # layout doesn't shift depending on where a photo came from.
        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            im = ImageOps.fit(im, (_WIDTH, _HEIGHT), method=Image.LANCZOS)
            im.save(out_path, format="PNG")
        return out_path

    raise _NoMatch(f"no Pexels result for '{name}'")


def _choose(
    photos: list[dict], name: str, description: str, settings: Settings
) -> dict | None:
    """Which of these photos is actually this dish? None if it's none of them.

    Validation is an improvement, not a dependency: if the vision call fails
    for any reason we fall back to the first result, which is exactly what this
    code did before the check existed. A broken checker must not stop a
    restaurant getting its menu online.
    """
    if not settings.validate_images or len(photos) == 1:
        return photos[0]

    try:
        index = _pick_best(photos, name, description, settings)
    except Exception as exc:  # noqa: BLE001 — degrade to the old behaviour
        print(f"    ! image check failed for '{name}', keeping first: {exc}")
        return photos[0]

    if index is None:
        print(f"    · rejected all {len(photos)} stock photos for '{name}'")
        return None
    if index:
        print(f"    · picked candidate {index + 1}/{len(photos)} for '{name}'")
    return photos[index]


def _pick_best(
    photos: list[dict], name: str, description: str, settings: Settings
) -> int | None:
    """Show the candidates to a vision model and return the index it picks.

    Thumbnails, not full images: `tiny` is ~280px, which is plenty to tell a
    biryani from a bowl of noodles, and keeps this to a few hundred KB per
    dish instead of several MB. They are downloaded once and reused across the
    whole chain, so escalating to another model costs no extra bandwidth.

    Walks `image_check_chain`, retrying each model while it reports a transient
    fault. Raises only if every model in the chain fails, which the caller
    treats as "keep the first result".
    """
    thumbs: list[bytes] = []
    for photo in photos:
        src = photo.get("src", {})
        thumb = src.get("tiny") or src.get("small") or src.get("medium")
        if not thumb:
            return 0  # a candidate we can't even look at; don't judge the set
        thumbs.append(_fetch(thumb, timeout=20))

    if not thumbs:
        return 0

    chain = settings.image_check_chain
    if not chain:
        return 0

    dish = f"{name}. {description}".strip().rstrip(".")
    prompt = _pick_prompt(dish, len(photos))

    last: Exception | None = None
    for provider, model in chain:
        for attempt in range(_CHECK_ATTEMPTS):
            try:
                return _ask_pick(provider, model, thumbs, prompt, len(photos), settings)
            except Exception as exc:  # noqa: BLE001 — the next model is the handler
                last = exc
                # Vision tiers return 503 in waves; without this the check
                # quietly does nothing for a whole publish run, which is the
                # worst outcome — the owner believes their photos were vetted.
                if _is_transient(exc) and attempt < _CHECK_ATTEMPTS - 1:
                    time.sleep(_CHECK_BACKOFF * (attempt + 1))
                    continue
                break  # permanent, or out of retries: escalate to the next model

    raise RuntimeError(f"all {len(chain)} check models failed: {last}")


def _pick_prompt(dish: str, count: int) -> str:
    return (
        f'An Indian restaurant is putting "{dish}" on its menu.\n\n'
        f"Which of the {count} photographs above shows that exact dish, as a "
        "customer in that restaurant would be served it?\n\n"
        "Be strict about regional dishes. A name can match in words while the "
        "photograph shows a different food from a different cuisine — South "
        "Indian filter coffee is not a pour-over brewing set, and a dish named "
        "for a place must look like that place's version of it. Judge what is "
        "in the picture, not what the words could mean.\n\n"
        "Reject a photograph that shows a different dish, raw ingredients "
        "instead of a prepared dish, a person, a restaurant interior, or "
        "anything unrelated. If the dish is vegetarian, reject photographs "
        "containing meat.\n\n"
        "Accuracy decides it. Only when two photographs are equally accurate "
        "should you prefer the clearer, more appetising one.\n\n"
        "Answer with the photograph's number, or 0 if none of them shows this "
        "dish. Answering 0 is the right call whenever you are unsure — the "
        "restaurant would rather have no photograph than a photograph of the "
        "wrong food."
    )


def _ask_pick(
    provider: str,
    model: str,
    thumbs: list[bytes],
    prompt: str,
    count: int,
    settings: Settings,
) -> int | None:
    """One model's verdict. Returns a 0-based index, or None for "none fit"."""
    if provider == "gemini":
        pick = _pick_gemini(model, thumbs, prompt, settings)
    elif provider == "openai":
        pick = _pick_openai(model, thumbs, prompt, settings)
    else:
        raise RuntimeError(f"Unknown image-check provider '{provider}'")

    if pick is None:
        return 0
    # The model answers 1..N, or 0 for "none of these".
    if pick.best_index <= 0 or pick.best_index > count:
        return None
    return pick.best_index - 1


def _pick_gemini(
    model: str, thumbs: list[bytes], prompt: str, settings: Settings
) -> _Pick | None:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.google_api_key)

    parts: list = []
    for position, raw in enumerate(thumbs, start=1):
        parts.append(types.Part.from_text(text=f"Photograph {position}:"))
        parts.append(types.Part.from_bytes(data=raw, mime_type="image/jpeg"))
    parts.append(types.Part.from_text(text=prompt))

    response = client.models.generate_content(
        model=model,
        contents=parts,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_Pick,
            # A judgement, not a creative task.
            temperature=0.0,
        ),
    )
    return response.parsed


def _pick_openai(
    model: str, thumbs: list[bytes], prompt: str, settings: Settings
) -> _Pick | None:
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)

    content: list[dict] = []
    for position, raw in enumerate(thumbs, start=1):
        content.append({"type": "text", "text": f"Photograph {position}:"})
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/jpeg;base64,"
                    + base64.standard_b64encode(raw).decode("utf-8")
                },
            }
        )
    content.append({"type": "text", "text": prompt})

    response = client.beta.chat.completions.parse(
        model=model,
        messages=[{"role": "user", "content": content}],
        response_format=_Pick,
        temperature=0.0,
    )
    return response.choices[0].message.parsed


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
