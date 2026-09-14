"""Trials and subscriptions.

Two dates on the owner decide everything: when the free month ends, and how
long they have paid for. Status is computed from those on every read rather
than stored, because a stored status is only as correct as the last job that
updated it — and a billing flag that silently goes stale locks paying
customers out of their own console.

The rule that matters more than any of this code: an expired owner loses the
CONSOLE, never their menu. A diner scanning a code at a table must never see
an error because a restaurant missed a renewal. Losing the ability to change
prices is enough pressure; taking a live restaurant offline mid-service is not
a payment reminder, it is a disaster you caused.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models import Owner

TRIAL_DAYS = 30
# After the trial or a paid term runs out, the console keeps working for a
# week. Renewals are a human process — someone is away, the transfer is slow —
# and a grace period costs nothing next to an owner locked out on a Sunday.
GRACE_DAYS = 7
# When to start saying something in the UI.
WARN_WITHIN_DAYS = 7

# When the public menu stops being readable.
#
# Locking the console alone is not leverage in this market: a small restaurant
# changes its prices two or three times a year, so an owner can ignore a
# locked editor indefinitely and lose nothing. The menu the diner sees is the
# thing they actually pay for, so that is what eventually stops working.
#
# Three weeks after expiry, and a fortnight after editing locks — long enough
# that nobody is caught out by a slow bank transfer or a fortnight away.
MENU_DIM_DAYS = 20


@dataclass(frozen=True)
class Subscription:
    status: str  # "trialing" | "active" | "grace" | "expired"
    expires_on: datetime | None
    days_left: int  # negative once past expiry; 0 on the last day
    can_edit: bool  # may they change menus?
    is_paid: bool  # have they ever paid?

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "expires_on": self.expires_on.isoformat() if self.expires_on else None,
            "days_left": self.days_left,
            "can_edit": self.can_edit,
            "is_paid": self.is_paid,
            "warn": self.status in {"grace", "expired"}
            or (self.days_left <= WARN_WITHIN_DAYS and self.status == "trialing"),
        }


def trial_end_from(start: datetime | None = None) -> datetime:
    return (start or datetime.now(timezone.utc)) + timedelta(days=TRIAL_DAYS)


def subscription_status(owner: Owner, now: datetime | None = None) -> Subscription:
    now = now or datetime.now(timezone.utc)

    # Paid time always wins, even if the trial date is in the past.
    if owner.paid_until and _aware(owner.paid_until) > now:
        return _build("active", _aware(owner.paid_until), now, can_edit=True, paid=True)

    if owner.trial_ends_at and _aware(owner.trial_ends_at) > now:
        return _build(
            "trialing", _aware(owner.trial_ends_at), now, can_edit=True, paid=False
        )

    # Whichever ran out most recently is the one the grace period hangs off.
    ended = max(
        [d for d in (_aware(owner.paid_until), _aware(owner.trial_ends_at)) if d],
        default=None,
    )

    # An owner from before billing existed has neither date. Treat that as a
    # trial starting now rather than as an expiry — a migration must never
    # lock someone out.
    if ended is None:
        return _build("trialing", trial_end_from(now), now, can_edit=True, paid=False)

    if now <= ended + timedelta(days=GRACE_DAYS):
        return _build("grace", ended, now, can_edit=True, paid=bool(owner.paid_until))

    return _build("expired", ended, now, can_edit=False, paid=bool(owner.paid_until))


def _build(
    status: str, expires: datetime, now: datetime, *, can_edit: bool, paid: bool
) -> Subscription:
    return Subscription(
        status=status,
        expires_on=expires,
        days_left=(expires - now).days,
        can_edit=can_edit,
        is_paid=paid,
    )


def menu_is_dimmed(owner: Owner, now: datetime | None = None) -> bool:
    """Should the diner's menu be obscured?

    Separate from `subscription_status` because it answers a question about a
    stranger's screen rather than the owner's, and because it must fail open:
    an owner with no dates at all, or any state that isn't clearly "expired
    weeks ago", keeps a working menu. The cost of dimming a paying
    restaurant's menu during service is far higher than the cost of serving a
    lapsed one for another day.
    """
    now = now or datetime.now(timezone.utc)
    sub = subscription_status(owner, now)
    if sub.status != "expired" or sub.expires_on is None:
        return False
    return now > sub.expires_on + timedelta(days=MENU_DIM_DAYS)


def _aware(value: datetime | None) -> datetime | None:
    """Postgres can hand back naive datetimes depending on the column type."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
