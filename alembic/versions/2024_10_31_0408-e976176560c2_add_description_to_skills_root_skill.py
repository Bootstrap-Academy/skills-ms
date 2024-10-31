"""Add description to skills_root_skill

Revision ID: e976176560c2
Create Date: 2024-10-31 04:08:11.984151
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e976176560c2"
down_revision = "1a9e957978e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skills_root_skill", sa.Column("description", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("skills_root_skill", "description")
