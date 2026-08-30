"""Restaurant endpoints.

Owner routes require a Supabase session and are scoped to that owner.
Public routes serve one thing: the menu a diner sees after scanning the QR.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.storage import StorageClient
from app.models import Owner, Restaurant
from app.modules.auth.service import current_owner
from app.modules.qr.service import generate_qr, generate_table_tent
from app.modules.restaurants import service
from app.modules.restaurants.logo import LogoError, process_logo
from app.modules.restaurants.publish import JOBS, PublishJob, run_publish
from app.schemas.menu import Menu

owner_router = APIRouter(prefix="/api", tags=["owner"])
public_router = APIRouter(prefix="/api/public", tags=["public"])


class RestaurantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    currency: str
    is_published: bool
    menu_url: str
    item_count: int = 0
    logo_url: str | None = None
    whatsapp: str | None = None


class CreateRestaurantIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    menu: Menu
    slug: str | None = None


class UpdateMenuIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    menu: Menu | None = None
    whatsapp: str | None = None


def _out(restaurant: Restaurant, settings: Settings, item_count: int = 0) -> RestaurantOut:
    return RestaurantOut(
        id=restaurant.id,
        name=restaurant.name,
        slug=restaurant.slug,
        currency=restaurant.currency,
        is_published=restaurant.is_published,
        menu_url=settings.menu_url_for(restaurant.slug),
        item_count=item_count,
        logo_url=restaurant.logo_url,
        whatsapp=restaurant.whatsapp,
    )


# --------------------------------------------------------------- owner routes


@owner_router.get("/me/restaurants", response_model=list[RestaurantOut])
async def my_restaurants(
    owner: Owner = Depends(current_owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[RestaurantOut]:
    return [
        _out(r, settings) for r in await service.list_for_owner(session, owner.id)
    ]


@owner_router.post(
    "/restaurants", response_model=RestaurantOut, status_code=status.HTTP_201_CREATED
)
async def create_restaurant(
    payload: CreateRestaurantIn,
    owner: Owner = Depends(current_owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RestaurantOut:
    if payload.slug:
        if not service.is_valid_slug(payload.slug):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "That web address isn't available. Use lowercase letters, "
                "numbers and hyphens.",
            )
        if await service.get_by_slug(session, payload.slug, published_only=False):
            raise HTTPException(status.HTTP_409_CONFLICT, "That web address is taken.")
        slug = payload.slug
    else:
        slug = await service.generate_slug(session, payload.name)

    restaurant = Restaurant(
        owner_id=owner.id,
        name=payload.name,
        slug=slug,
        currency=payload.menu.currency or "INR",
    )
    session.add(restaurant)
    await session.flush()
    await service.replace_menu(session, restaurant, payload.menu)

    count = sum(len(s.items) for s in payload.menu.sections)
    return _out(restaurant, settings, count)


@owner_router.get("/restaurants/{restaurant_id}")
async def read_restaurant(
    restaurant_id: uuid.UUID,
    owner: Owner = Depends(current_owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    restaurant = await service.get_for_owner(session, restaurant_id, owner.id)
    if restaurant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We couldn't find that menu.")
    menu = service.to_schema(restaurant)
    return {
        "restaurant": _out(
            restaurant, settings, sum(len(s.items) for s in menu.sections)
        ).model_dump(mode="json"),
        "menu": menu.model_dump(mode="json"),
    }


@owner_router.patch("/restaurants/{restaurant_id}", response_model=RestaurantOut)
async def update_restaurant(
    restaurant_id: uuid.UUID,
    payload: UpdateMenuIn,
    owner: Owner = Depends(current_owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RestaurantOut:
    restaurant = await service.get_for_owner(session, restaurant_id, owner.id)
    if restaurant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We couldn't find that menu.")

    if payload.name:
        restaurant.name = payload.name
    if payload.whatsapp is not None:
        restaurant.whatsapp = payload.whatsapp or None
    if payload.menu is not None:
        await service.replace_menu(session, restaurant, payload.menu)

    await session.flush()
    count = (
        sum(len(s.items) for s in payload.menu.sections)
        if payload.menu
        else sum(len(s.items) for s in restaurant.sections)
    )
    return _out(restaurant, settings, count)


@owner_router.post("/restaurants/{restaurant_id}/logo")
async def upload_logo(
    restaurant_id: uuid.UUID,
    file: UploadFile = File(...),
    owner: Owner = Depends(current_owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Upload the restaurant's logo. Any common image format is fine."""
    restaurant = await service.get_for_owner(session, restaurant_id, owner.id)
    if restaurant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We couldn't find that menu.")

    try:
        png = process_logo(await file.read())
    except LogoError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    storage = StorageClient.from_settings(settings)
    storage.ensure_bucket()
    # A stable path per restaurant, cache-busted so a replaced logo shows up
    # immediately instead of sitting behind the CDN's copy of the old one.
    url = storage.upload(f"{restaurant.id}/logo.png", png)
    restaurant.logo_url = f"{url}?v={uuid.uuid4().hex[:8]}"
    await session.flush()

    return {"logo_url": restaurant.logo_url}


@owner_router.delete("/restaurants/{restaurant_id}/logo", status_code=status.HTTP_204_NO_CONTENT)
async def remove_logo(
    restaurant_id: uuid.UUID,
    owner: Owner = Depends(current_owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> None:
    restaurant = await service.get_for_owner(session, restaurant_id, owner.id)
    if restaurant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We couldn't find that menu.")
    restaurant.logo_url = None
    await session.flush()
    try:
        StorageClient.from_settings(settings).remove_prefix(f"{restaurant.id}/logo.png")
    except Exception as exc:
        print(f"  ! could not delete logo file: {exc}")


class PublishIn(BaseModel):
    generate_images: bool = True
    max_images: int = 0  # 0 = every dish that still needs a photo
    replace_existing: bool = False


@owner_router.post("/restaurants/{restaurant_id}/publish", status_code=status.HTTP_202_ACCEPTED)
async def publish_restaurant(
    restaurant_id: uuid.UUID,
    payload: PublishIn,
    background: BackgroundTasks,
    owner: Owner = Depends(current_owner),
    session: AsyncSession = Depends(get_session),
) -> dict:
    restaurant = await service.get_for_owner(session, restaurant_id, owner.id)
    if restaurant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We couldn't find that menu.")

    job = PublishJob(id=uuid.uuid4().hex[:12], restaurant_id=restaurant.id)
    JOBS[job.id] = job
    background.add_task(
        run_publish,
        job.id,
        restaurant.id,
        generate_images=payload.generate_images,
        max_images=payload.max_images,
        replace_existing=payload.replace_existing,
    )
    return job.as_dict()


@owner_router.get("/publish-jobs/{job_id}")
async def publish_status(job_id: str, owner: Owner = Depends(current_owner)) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That job has expired.")
    return job.as_dict()


@owner_router.get("/restaurants/{restaurant_id}/qr.png")
async def restaurant_qr(
    restaurant_id: uuid.UUID,
    tent: bool = False,
    owner: Owner = Depends(current_owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    """QR code, or the printable table card when ?tent=true.

    Generated on demand rather than stored — it's cheap, and it always
    reflects the restaurant's current web address.
    """
    restaurant = await service.get_for_owner(session, restaurant_id, owner.id)
    if restaurant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We couldn't find that menu.")

    url = settings.menu_url_for(restaurant.slug)
    with tempfile.TemporaryDirectory(prefix="menusnap_qr_") as tmp:
        out = Path(tmp) / "qr.png"
        if tent:
            generate_table_tent(url, restaurant.name, out)
        else:
            generate_qr(url, out)
        data = out.read_bytes()

    filename = f"{restaurant.slug}-{'table-card' if tent else 'qr'}.png"
    return Response(
        content=data,
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@owner_router.delete("/restaurants/{restaurant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_restaurant(
    restaurant_id: uuid.UUID,
    owner: Owner = Depends(current_owner),
    session: AsyncSession = Depends(get_session),
) -> None:
    restaurant = await service.get_for_owner(session, restaurant_id, owner.id)
    if restaurant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We couldn't find that menu.")

    # Drop the dish photos too, or deleted menus quietly consume storage forever.
    try:
        StorageClient.from_settings(get_settings()).remove_prefix(str(restaurant.id))
    except Exception as exc:  # never block the delete on a storage hiccup
        print(f"  ! could not clear images for {restaurant.slug}: {exc}")

    await session.delete(restaurant)


# -------------------------------------------------------------- public routes


@public_router.get("/menus/{slug}")
async def public_menu(
    slug: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """What a diner gets after scanning the QR."""
    restaurant = await service.get_by_slug(session, slug)
    if restaurant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No menu published here yet.")

    menu = service.to_schema(restaurant)
    return {
        "slug": restaurant.slug,
        "name": restaurant.name,
        "logo_url": restaurant.logo_url,
        "whatsapp": restaurant.whatsapp,
        "menu": menu.model_dump(mode="json"),
        "images": {
            f"{s_idx}-{i_idx}": item.image_url
            for s_idx, section in enumerate(restaurant.sections)
            for i_idx, item in enumerate(section.items)
            if item.image_url
        },
    }


@public_router.get("/slug-available/{slug}")
async def slug_available(slug: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Live check for the 'choose your web address' field."""
    if not service.is_valid_slug(slug):
        return {"available": False, "reason": "not_allowed"}
    taken = await service.get_by_slug(session, slug, published_only=False) is not None
    return {"available": not taken, "reason": "taken" if taken else None}
