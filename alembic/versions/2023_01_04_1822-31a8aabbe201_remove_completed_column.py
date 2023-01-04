"""Remove completed column

Revision ID: 31a8aabbe201
Create Date: 2023-01-04 18:22:50.139235
"""

from alembic import op

import sqlalchemy as sa
from sqlalchemy.dialects import mysql


# revision identifiers, used by Alembic.
revision = "31a8aabbe201"
down_revision = "f450fab91cb3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("skills_xp", "completed")


def downgrade() -> None:
    op.add_column("skills_xp", sa.Column("completed", sa.Boolean(), nullable=True))
