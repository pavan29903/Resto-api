"""The access rule, as a FastAPI dependency.

Applied to routes that CHANGE a menu, and deliberately not to routes that read
one, print a QR code, or delete a restaurant:

  - reads stay open so an owner can still see what they have and get support
  - the QR stays printable, because it points at a menu that is still live
  - deleting stays possible, because holding someone's data hostage over a
    late payment is not leverage, it is just unpleasant

And nothing here touches the public menu routes at all. A diner scanning a
code at a table must never see an error because a restaurant missed a renewal.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.models import Owner
from app.modules.auth.service import current_owner
from app.modules.billing.service import subscription_status


async def editing_owner(owner: Owner = Depends(current_owner)) -> Owner:
    """The signed-in owner, if their trial or subscription still allows edits."""
    sub = subscription_status(owner)
    if not sub.can_edit:
        # 402 rather than 403: this is not a permissions problem and the fix
        # is not "ask someone for access" — it says exactly what happened.
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "Your free month has ended, so menus can't be changed for now. "
            "Your menu is still live and your codes still work — message us on "
            "WhatsApp to carry on.",
        )
    return owner
