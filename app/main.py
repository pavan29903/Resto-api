"""RestoFood API.

Owner routes are authenticated with a Supabase session and scoped to that
owner; public routes serve the menu a diner sees after scanning a QR code.

Menus live in Postgres and dish photos in object storage — nothing is written
to local disk, so any container can serve any request.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings, get_settings
from app.models import Owner
from app.modules.auth.service import current_owner
from app.modules.extraction.service import MEDIA_TYPES, ProviderBusy, extract_menu
from app.modules.restaurants.router import owner_router, public_router

logger = logging.getLogger(__name__)

app = FastAPI(title="RestoFood API", version="0.2.0")

# The frontend is a separate origin, and once a domain is attached each
# restaurant is a separate origin again (<slug>.restofood.in). Both come from
# settings so production never has to allow a wildcard like *.vercel.app,
# which would let anyone's deployment call this API with a user's session.
_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.allowed_origins,
    allow_origin_regex=_settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(owner_router)
app.include_router(public_router)


@app.get("/api/config")
def read_config(settings: Settings = Depends(get_settings)) -> dict:
    """Which providers are live — shown in the dashboard's status strip."""
    chain = settings.extraction_chain
    return {
        "extraction_provider": settings.extraction_provider,
        "extraction_model": chain[0][1] if chain else None,
        # Every provider that would actually be tried, in order. A key that
        # isn't set is already filtered out, so this says what will happen
        # rather than what was configured.
        "extraction_chain": [f"{p}:{m}" for p, m in chain],
        "image_provider": settings.image_provider,
        "image_fallback": settings.image_fallback,
        "menu_domain": settings.menu_domain,
        "has_extraction_key": bool(chain),
        "has_image_key": bool(settings.pexels_api_key),
    }


@app.post("/api/extract")
async def api_extract(
    files: list[UploadFile] = File(...),
    owner: Owner = Depends(current_owner),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Menu photo(s) -> structured menu.

    Authenticated because each call spends money at the vision provider.
    Nothing is persisted here — the owner reviews the result and only then
    creates a restaurant from it.
    """
    if not files:
        raise HTTPException(400, "Choose at least one photo of your menu card.")

    tmp_dir = Path(tempfile.mkdtemp(prefix="restofood_"))
    try:
        saved: list[Path] = []
        for upload in files:
            suffix = Path(upload.filename or "photo.jpg").suffix.lower()
            if suffix not in MEDIA_TYPES:
                raise HTTPException(
                    400,
                    f"'{suffix}' files aren't supported. "
                    f"Use {', '.join(sorted(MEDIA_TYPES))}.",
                )
            dest = tmp_dir / (upload.filename or f"photo{suffix}")
            dest.write_bytes(await upload.read())
            saved.append(dest)

        # Extraction is a blocking call that runs for tens of seconds. Called
        # directly from this async function it would stall the event loop and
        # freeze every other request on the instance — which on a single free
        # worker means the whole API.
        menu = await run_in_threadpool(extract_menu, saved, settings)
        return {
            "menu": menu.model_dump(mode="json"),
            "item_count": sum(len(s.items) for s in menu.sections),
        }
    except HTTPException:
        raise
    except ProviderBusy as exc:
        # 503, not 500: nothing here is broken and the next attempt will
        # probably work. The distinction matters to the owner reading the
        # message and to anything that retries on our behalf.
        logger.warning("Extraction unavailable: %s", exc)
        raise HTTPException(
            503,
            "The menu reader is busy right now. Please try again in a moment — "
            "your photo is fine.",
        ) from exc
    except Exception as exc:
        # The provider's raw error is for the log, not for a restaurant owner.
        logger.exception("Menu extraction failed")
        raise HTTPException(
            500,
            "We couldn't read that menu. Try again, or use a clearer photo of "
            "the card.",
        ) from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/health")
def health() -> dict:
    return {"ok": True}
