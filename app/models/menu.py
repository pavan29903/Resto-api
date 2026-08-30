from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.restaurant import Restaurant


class MenuSection(Base, TimestampMixin):
    __tablename__ = "menu_sections"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    restaurant: Mapped["Restaurant"] = relationship(back_populates="sections")
    items: Mapped[list["MenuItem"]] = relationship(
        back_populates="section",
        cascade="all, delete-orphan",
        order_by="MenuItem.sort_order",
    )


class MenuItem(Base, TimestampMixin):
    __tablename__ = "menu_items"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    section_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("menu_sections.id", ondelete="CASCADE"), index=True
    )

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    # Numeric, not float — money should not be stored in binary floating point.
    price: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)

    is_vegetarian: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    spice_level: Mapped[str | None] = mapped_column(String(20), nullable=True)

    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Where the photo came from: "pexels" | "ai" | "owner" | "placeholder".
    # Drives the "this is a stock photo — use your own" nudge in the dashboard,
    # and lets us re-generate only what the owner hasn't personalised.
    image_source: Mapped[str | None] = mapped_column(String(20), nullable=True)

    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    section: Mapped["MenuSection"] = relationship(back_populates="items")
