"""add completed column to xp table

Revision ID: efd84fdce9fc
Create Date: 2022-09-30 22:29:55.630520
"""

from alembic import op

import sqlalchemy as sa

from api import models


# revision identifiers, used by Alembic.
revision = "efd84fdce9fc"
down_revision = "e18492ffc92e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skills_xp", sa.Column("completed", sa.Boolean(), nullable=True))

    op.execute(sa.update(models.XP).values(completed=False))


def downgrade() -> None:
    op.drop_column("skills_xp", "completed")
