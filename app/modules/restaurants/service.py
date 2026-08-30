"""Restaurants and their menus, persisted.

Menus used to live as menu.json on disk. That cannot survive a deploy — a
container's filesystem is temporary — so everything now round-trips through
Postgres. This module is the only place that translates between the API's
Menu schema and the ORM rows.
"""

from __future__ import annotations

import re
import uuid

from slugify import slugify
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import MenuItem, MenuSection, Restaurant
from app.schemas.menu import Menu, MenuItem as MenuItemSchema, MenuSection as MenuSectionSchema

# Subdomains we keep for ourselves. A restaurant claiming "api" or "www" would
# shadow our own hosts, so these can never become a slug.
RESERVED_SLUGS = {
    "www", "api", "app", "admin", "dashboard", "mail", "smtp", "ftp", "cdn",
    "static", "assets", "blog", "help", "support", "status", "docs", "menu",
    "menus", "auth", "login", "signup", "account", "billing", "test", "staging",
    "dev", "demo", "internal", "root", "system",
}

# DNS labels: lowercase alphanumerics and hyphens, no leading/trailing hyphen,
# 63 characters maximum.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def is_valid_slug(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug)) and slug not in RESERVED_SLUGS


async def generate_slug(session: AsyncSession, name: str) -> str:
    """A free, DNS-legal subdomain derived from the restaurant's name."""
    base = slugify(name, max_length=55) or "menu"
    if base in RESERVED_SLUGS:
        base = f"{base}-cafe"

    candidate = base
    suffix = 2
    while True:
        taken = (
            await session.execute(
                select(func.count()).select_from(Restaurant).where(Restaurant.slug == candidate)
            )
        ).scalar_one()
        if not taken and is_valid_slug(candidate):
            return candidate
        candidate = f"{base}-{suffix}"[:63].rstrip("-")
        suffix += 1


async def get_by_slug(
    session: AsyncSession, slug: str, *, published_only: bool = True
) -> Restaurant | None:
    stmt = (
        select(Restaurant)
        .where(Restaurant.slug == slug)
        .options(selectinload(Restaurant.sections).selectinload(MenuSection.items))
    )
    if published_only:
        stmt = stmt.where(Restaurant.is_published.is_(True))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_for_owner(
    session: AsyncSession, restaurant_id: uuid.UUID, owner_id: uuid.UUID
) -> Restaurant | None:
    """Scoped by owner — this is what stops one owner reading another's menu."""
    return (
        await session.execute(
            select(Restaurant)
            .where(Restaurant.id == restaurant_id, Restaurant.owner_id == owner_id)
            .options(selectinload(Restaurant.sections).selectinload(MenuSection.items))
        )
    ).scalar_one_or_none()


async def list_for_owner(session: AsyncSession, owner_id: uuid.UUID) -> list[Restaurant]:
    return list(
        (
            await session.execute(
                select(Restaurant)
                .where(Restaurant.owner_id == owner_id)
                .order_by(Restaurant.created_at.desc())
            )
        ).scalars()
    )


async def replace_menu(session: AsyncSession, restaurant: Restaurant, menu: Menu) -> None:
    """Overwrite a restaurant's menu with the owner's reviewed version.

    Images are re-attached by position where the dish name is unchanged, so
    editing a price never silently discards a photo the owner uploaded.

    Everything here goes through explicit queries rather than the `sections`
    relationship: touching a relationship on a persistent object triggers a
    lazy load, which is illegal under asyncio and fails with MissingGreenlet.
    """
    existing = (
        await session.execute(
            select(MenuSection)
            .where(MenuSection.restaurant_id == restaurant.id)
            .options(selectinload(MenuSection.items))
            .order_by(MenuSection.sort_order)
        )
    ).scalars().all()

    keep: dict[tuple[int, str], tuple[str | None, str | None]] = {}
    for s_idx, section in enumerate(existing):
        for item in section.items:
            keep[(s_idx, item.name.strip().lower())] = (item.image_url, item.image_source)

    if existing:
        # menu_items has ON DELETE CASCADE on section_id, so removing the
        # sections clears their dishes in one statement.
        await session.execute(
            delete(MenuSection).where(MenuSection.restaurant_id == restaurant.id)
        )
    await session.flush()

    restaurant.currency = menu.currency or restaurant.currency

    for s_idx, section_in in enumerate(menu.sections):
        section = MenuSection(
            restaurant_id=restaurant.id, name=section_in.name, sort_order=s_idx
        )
        session.add(section)
        await session.flush()

        for i_idx, item_in in enumerate(section_in.items):
            image_url, image_source = keep.get(
                (s_idx, item_in.name.strip().lower()), (None, None)
            )
            session.add(
                MenuItem(
                    section_id=section.id,
                    name=item_in.name,
                    description=item_in.description or "",
                    price=item_in.price,
                    is_vegetarian=item_in.is_vegetarian,
                    spice_level=item_in.spice_level,
                    image_url=image_url,
                    image_source=image_source,
                    sort_order=i_idx,
                )
            )
    await session.flush()

    # The bulk DELETE above bypasses the identity map, so a `sections`
    # collection loaded earlier in this session still points at removed rows.
    # Expire it: the next access reloads and sees what we just wrote.
    session.expire(restaurant, ["sections"])


def to_schema(restaurant: Restaurant) -> Menu:
    """ORM rows -> the Menu shape the frontend already speaks."""
    return Menu(
        restaurant_name=restaurant.name,
        currency=restaurant.currency,
        sections=[
            MenuSectionSchema(
                name=section.name,
                items=[
                    MenuItemSchema(
                        name=item.name,
                        description=item.description or "",
                        price=float(item.price) if item.price is not None else None,
                        is_vegetarian=item.is_vegetarian,
                        spice_level=item.spice_level,
                    )
                    for item in section.items
                ],
            )
            for section in restaurant.sections
        ],
    )
