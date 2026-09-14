from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.menu import MenuSection


class Owner(Base, TimestampMixin):
    """A cafe owner.

    Passwords and sessions are Supabase Auth's job — we only keep the id it
    issues, plus enough profile to address them in the UI.
    """

    __tablename__ = "owners"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The `sub` claim from the Supabase JWT. Unique, and how we look owners up.
    auth_user_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Billing is two dates and nothing else. A stored status column would need
    # a job to keep it true and would be wrong the moment that job failed;
    # these two are facts, and the status is derived from them on read.
    #
    # Nullable because owners created before billing existed have neither, and
    # `subscription_status` treats that as "never started" rather than
    # "expired" — nobody gets locked out by a migration.
    trial_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set by hand today, by a payment webhook later. Either way it is the only
    # thing that says someone has paid.
    paid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    restaurants: Mapped[list["Restaurant"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


class Restaurant(Base, TimestampMixin):
    """One cafe. `slug` is the subdomain: <slug>.restofood.in"""

    __tablename__ = "restaurants"
    __table_args__ = (UniqueConstraint("slug", name="uq_restaurants_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("owners.id", ondelete="CASCADE"), index=True
    )

    name: Mapped[str] = mapped_column(String(200))
    # Doubles as the public subdomain, so it must survive DNS: lowercase
    # letters, digits and hyphens only. Enforced in the service layer.
    slug: Mapped[str] = mapped_column(String(63), index=True)

    currency: Mapped[str] = mapped_column(String(3), default="INR")
    whatsapp: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # The cafe's own logo, shown at the top of their menu. Stored as a public
    # Storage URL; the diner's phone loads it directly.
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # A menu exists as a draft until the owner has checked it. Only published
    # restaurants answer on their subdomain.
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    owner: Mapped["Owner"] = relationship(back_populates="restaurants")
    sections: Mapped[list["MenuSection"]] = relationship(
        back_populates="restaurant",
        cascade="all, delete-orphan",
        order_by="MenuSection.sort_order",
    )
