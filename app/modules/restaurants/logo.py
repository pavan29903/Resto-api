"""Restaurant logo handling.

Owners upload whatever they have — a PNG export, a phone photo of their board,
a WhatsApp JPEG. This normalises all of it into one small, transparent-safe
PNG so the menu header looks the same regardless of what arrived.

Everything is re-encoded through Pillow rather than stored as uploaded. That
is deliberate: it strips EXIF, and it means a file that merely *claims* to be
an image never reaches storage.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps, UnidentifiedImageError

# A logo never needs to be larger than this on a phone header; anything bigger
# is just slower to load on a restaurant's 4G connection.
MAX_EDGE = 512
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

ACCEPTED = {"PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"}


class LogoError(ValueError):
    """The upload isn't a usable logo. The message is shown to the owner."""


def process_logo(raw: bytes) -> bytes:
    """Validate and normalise an uploaded logo into a PNG. Raises LogoError."""
    if not raw:
        raise LogoError("That file was empty. Try choosing it again.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise LogoError("That image is larger than 5 MB. Try a smaller file.")

    try:
        with Image.open(io.BytesIO(raw)) as img:
            fmt = (img.format or "").upper()
            if fmt not in ACCEPTED:
                raise LogoError(
                    f"{fmt or 'That file type'} isn't supported. "
                    "Use a PNG, JPG or WEBP."
                )
            # Verify then reopen: verify() leaves the image unusable.
            img.verify()
    except UnidentifiedImageError as exc:
        raise LogoError("That file isn't an image we can read.") from exc
    except LogoError:
        raise
    except Exception as exc:
        raise LogoError("We couldn't read that image.") from exc

    with Image.open(io.BytesIO(raw)) as img:
        # Honour the camera's rotation flag, or phone photos arrive sideways.
        img = ImageOps.exif_transpose(img)

        # RGBA keeps a transparent logo transparent; a flat JPEG is unaffected.
        img = img.convert("RGBA")

        # Only ever shrink. Upscaling a small logo just blurs it.
        if max(img.size) > MAX_EDGE:
            img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
