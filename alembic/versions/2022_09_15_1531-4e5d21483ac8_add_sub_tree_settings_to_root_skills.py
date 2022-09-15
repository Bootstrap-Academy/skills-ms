"""add sub tree settings to root skills

Revision ID: 4e5d21483ac8
Create Date: 2022-09-15 15:31:12.243062
"""

from alembic import op

import sqlalchemy as sa

from api import models


# revision identifiers, used by Alembic.
revision = "4e5d21483ac8"
down_revision = "e4a1b873b65a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skills_root_skill", sa.Column("sub_tree_rows", sa.Integer(), nullable=True))
    op.add_column("skills_root_skill", sa.Column("sub_tree_columns", sa.Integer(), nullable=True))

    op.execute(sa.update(models.RootSkill).values(sub_tree_rows=20, sub_tree_columns=20))


def downgrade() -> None:
    op.drop_column("skills_root_skill", "sub_tree_columns")
    op.drop_column("skills_root_skill", "sub_tree_rows")
