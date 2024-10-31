"""Add description to skills_sub_skill

Revision ID: 92dd17d5ba45
Create Date: 2024-10-31 04:08:26.375163
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "92dd17d5ba45"
down_revision = "e976176560c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skills_sub_skill", sa.Column("description", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("skills_sub_skill", "description")
