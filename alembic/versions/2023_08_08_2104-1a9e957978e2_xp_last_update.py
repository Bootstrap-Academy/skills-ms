"""xp last_update

Revision ID: 1a9e957978e2
Create Date: 2023-08-08 21:04:09.339320
"""

from alembic import op

import sqlalchemy as sa

from api.database.database import UTCDateTime


# revision identifiers, used by Alembic.
revision = "1a9e957978e2"
down_revision = "31a8aabbe201"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skills_xp", sa.Column("last_update", UTCDateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("skills_xp", "last_update")
