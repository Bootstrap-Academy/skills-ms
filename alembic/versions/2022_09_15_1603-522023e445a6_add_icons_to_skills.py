"""add icons to skills

Revision ID: 522023e445a6
Create Date: 2022-09-15 16:03:17.753867
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "522023e445a6"
down_revision = "4e5d21483ac8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skills_root_skill", sa.Column("icon", sa.String(length=256), nullable=True))
    op.add_column("skills_sub_skill", sa.Column("icon", sa.String(length=256), nullable=True))


def downgrade() -> None:
    op.drop_column("skills_sub_skill", "icon")
    op.drop_column("skills_root_skill", "icon")
