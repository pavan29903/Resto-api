"""Billing endpoints.

Three, and no more than three at this stage:

  - the owner asks how long they have left
  - you mark someone paid after the money arrives
  - you ask who to message this week

There is deliberately no checkout here. Nobody pays on day one — they take the
free month first — so the collection problem does not exist until a trial ends,
and at that point a payment link sent over WhatsApp does the job without any
integration to build, break, or reconcile.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.models import Owner, Restaurant
from app.modules.auth.service import current_owner
from app.modules.billing import service as billing

router = APIRouter(prefix="/api", tags=["billing"])


@router.get("/me/subscription")
async def my_subscription(owner: Owner = Depends(current_owner)) -> dict:
    """What the console's trial banner reads."""
    return billing.subscription_status(owner).as_dict()


def _check_admin(token: str | None, settings: Settings) -> None:
    """Guard for the two endpoints that are for you, not for customers.

    `compare_digest` rather than `==` so the comparison doesn't leak the
    token's length or prefix through timing. An unset secret disables these
    endpoints entirely — a blank password must never mean "open to all".
    """
    secret = settings.admin_token
    if not secret:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Not found."
        )
    if not token or not hmac.compare_digest(token, secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authorised.")


@router.post("/internal/owners/{email}/paid")
async def mark_paid(
    email: str,
    months: int = 6,
    x_admin_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Record that someone has paid, for `months` from now.

    Extends from whichever is later — today, or their current expiry — so
    renewing early adds time instead of throwing away what they already had.
    """
    _check_admin(x_admin_token, settings)

    owner = (
        await session.execute(select(Owner).where(Owner.email == email))
    ).scalar_one_or_none()
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No owner with email {email}.")

    now = datetime.now(timezone.utc)
    current = owner.paid_until
    if current and current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    start = max(now, current) if current else now
    owner.paid_until = start + timedelta(days=30 * months)
    await session.commit()

    return {"email": owner.email, **billing.subscription_status(owner).as_dict()}


def _normalise_phone(raw: str | None) -> str | None:
    """Digits only, with India's country code assumed for a bare 10-digit number.

    Stored in the shape wa.me wants, so building a link is never more than
    string concatenation. A number typed as "+91 84669 01383", "084669 01383"
    or "8466901383" all end up identical.
    """
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    digits = digits.lstrip("0")
    if len(digits) == 10:  # a bare Indian mobile
        digits = f"91{digits}"
    return digits[:20]


@router.post("/internal/restaurants/{slug}/handover")
async def handover(
    slug: str,
    email: str,
    phone: str | None = None,
    x_admin_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Give a restaurant you built to the person who runs it.

    The selling motion this exists for: you walk into a cafe, photograph the
    menu card on your own account, and show them their menu on your phone
    before they have agreed to anything. When they say yes, this hands it over
    — without deleting anything or asking them to redo the work.

    The menu, the slug, the photographs and the QR code are all untouched. Only
    who owns it changes, so a code already printed keeps working.

    They need not have an account yet. A placeholder owner is created for their
    email and the first sign-in with that verified address claims it, so you
    can hand over and leave rather than standing over them during a signup.
    """
    _check_admin(x_admin_token, settings)

    email = email.strip().lower()
    if "@" not in email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That isn't an email address.")

    restaurant = (
        await session.execute(select(Restaurant).where(Restaurant.slug == slug))
    ).scalar_one_or_none()
    if restaurant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No restaurant at '{slug}'.")

    target = (
        await session.execute(select(Owner).where(Owner.email == email))
    ).scalar_one_or_none()

    created = target is None
    if target is None:
        # No auth id: this row is a placeholder until they sign in. Their free
        # month starts now, since that is when they got a working menu.
        target = Owner(
            auth_user_id=None,
            email=email,
            phone=_normalise_phone(phone),
            trial_ends_at=billing.trial_end_from(),
        )
        session.add(target)
        await session.flush()
    elif phone:
        # They already exist and you've just learned their number — or a
        # better one. Never blank an existing number with an empty field.
        target.phone = _normalise_phone(phone) or target.phone

    if restaurant.owner_id == target.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{slug} already belongs to {email}."
        )

    restaurant.owner_id = target.id
    await session.commit()

    return {
        "slug": restaurant.slug,
        "now_owned_by": email,
        "phone": target.phone,
        "account_existed": not created,
        "awaiting_signup": target.auth_user_id is None,
        **billing.subscription_status(target).as_dict(),
    }


@router.get("/internal/expiring")
async def expiring(
    within_days: int = 7,
    x_admin_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Who to message on WhatsApp this week.

    The whole trial-reminder feature, for as long as you have few enough
    customers to message by hand — which is longer than it sounds, and works
    far better than an automated email this customer would never open.
    """
    _check_admin(x_admin_token, settings)

    owners = (await session.execute(select(Owner))).scalars().all()

    soon = []
    for owner in owners:
        sub = billing.subscription_status(owner)
        if sub.status in {"grace", "expired"} or (
            sub.status == "trialing" and sub.days_left <= within_days
        ):
            soon.append(
                {
                    "email": owner.email,
                    "phone": owner.phone,
                    "status": sub.status,
                    "days_left": sub.days_left,
                    "expires_on": sub.expires_on.isoformat() if sub.expires_on else None,
                }
            )

    soon.sort(key=lambda row: row["days_left"])
    return {"count": len(soon), "owners": soon}
