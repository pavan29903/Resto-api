"""MenuSnap API.

Owner routes are authenticated with a Supabase session and scoped to that
owner; public routes serve the menu a diner sees after scanning a QR code.

Menus live in Postgres and dish photos in object storage — nothing is written
to local disk, so any container can serve any request.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.models import Owner
from app.modules.auth.service import current_owner
from app.modules.extraction.service import MEDIA_TYPES, extract_menu
from app.modules.restaurants.router import owner_router, public_router

app = FastAPI(title="MenuSnap API", version="0.2.0")

# The Next.js frontend runs on its own origin. Narrow this to the deployed
# frontend origin before going live.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(owner_router)
app.include_router(public_router)


@app.get("/api/config")
def read_config(settings: Settings = Depends(get_settings)) -> dict:
    """Which providers are live — shown in the dashboard's status strip."""
    return {
        "extraction_provider": settings.extraction_provider,
        "extraction_model": (
            settings.gemini_model
            if settings.extraction_provider == "gemini"
            else settings.extraction_model
        ),
        "image_provider": settings.image_provider,
        "image_fallback": settings.image_fallback,
        "menu_domain": settings.menu_domain,
        "has_extraction_key": bool(
            settings.google_api_key
            if settings.extraction_provider == "gemini"
            else settings.anthropic_api_key
        ),
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

    tmp_dir = Path(tempfile.mkdtemp(prefix="menusnap_"))
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

        menu = extract_menu(saved, settings)
        return {
            "menu": menu.model_dump(mode="json"),
            "item_count": sum(len(s.items) for s in menu.sections),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"We couldn't read that menu: {exc}") from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/health")
def health() -> dict:
    return {"ok": True}
