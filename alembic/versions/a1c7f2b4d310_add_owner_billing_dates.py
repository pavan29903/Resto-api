"""Add owner billing dates

Two nullable timestamps on owners: when the free trial runs out, and how long
they have paid for. Deliberately no status column — status is derived on read,
so it cannot drift out of date when a job fails to run.

Nullable on purpose. Owners who existed before billing get NULL for both, and
`subscription_status` reads that as "never started" rather than "expired", so
applying this migration cannot lock anyone out of their own console.

Revision ID: a1c7f2b4d310
Revises: e4e545548c78
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1c7f2b4d310"
down_revision: Union[str, None] = "e4e545548c78"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "owners",
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "owners",
        sa.Column("paid_until", sa.DateTime(timezone=True), nullable=True),
    )

    # Anyone already using the product gets a full free month from today rather
    # than being treated as a lapsed trial.
    op.execute(
        "UPDATE owners SET trial_ends_at = now() + interval '30 days' "
        "WHERE trial_ends_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("owners", "paid_until")
    op.drop_column("owners", "trial_ends_at")
