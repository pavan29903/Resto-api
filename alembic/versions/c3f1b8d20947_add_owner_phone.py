"""Add a contact phone number to owners

The number you chase a renewal on. Separate from `restaurants.whatsapp`, which
is printed on the menu for diners — an owner may not want their personal number
shown to everyone who scans a code.

Revision ID: c3f1b8d20947
Revises: b2e9a4c7f105
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3f1b8d20947"
down_revision: Union[str, None] = "b2e9a4c7f105"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("owners", sa.Column("phone", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("owners", "phone")
