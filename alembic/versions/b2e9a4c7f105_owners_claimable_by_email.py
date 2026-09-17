"""Let an owner row exist before its person has an account

`auth_user_id` becomes nullable so a restaurant can be built and handed over
before the owner has signed up. Until they do, the row is a placeholder holding
their menu; the first sign-in with that verified email claims it.

Postgres allows many NULLs under a UNIQUE constraint, so any number of
placeholders can coexist.

Revision ID: b2e9a4c7f105
Revises: a1c7f2b4d310
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2e9a4c7f105"
down_revision: Union[str, None] = "a1c7f2b4d310"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "owners",
        "auth_user_id",
        existing_type=sa.String(length=128),
        nullable=True,
    )
    # Two owners must never share an email, or claiming one by email would be
    # ambiguous. Unenforced until now because the auth id was the only key.
    op.create_index("uq_owners_email", "owners", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_owners_email", table_name="owners")
    # Rows never claimed have no auth id and cannot satisfy NOT NULL.
    op.execute("DELETE FROM owners WHERE auth_user_id IS NULL")
    op.alter_column(
        "owners",
        "auth_user_id",
        existing_type=sa.String(length=128),
        nullable=False,
    )
