"""add rows and columns to skill tables

Revision ID: 10d85d8640cc
Create Date: 2022-09-15 14:25:56.303816
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "10d85d8640cc"
down_revision = "a7c2f8ffc641"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skills_root_skill", sa.Column("row", sa.Integer(), nullable=True))
    op.add_column("skills_root_skill", sa.Column("column", sa.Integer(), nullable=True))
    op.add_column("skills_sub_skill", sa.Column("row", sa.Integer(), nullable=True))
    op.add_column("skills_sub_skill", sa.Column("column", sa.Integer(), nullable=True))

    op.execute("UPDATE skills_root_skill SET `row` = 0, `column` = 0;")
    op.execute("UPDATE skills_sub_skill SET `row` = 0, `column` = 0;")


def downgrade() -> None:
    op.drop_column("skills_sub_skill", "column")
    op.drop_column("skills_sub_skill", "row")
    op.drop_column("skills_root_skill", "column")
    op.drop_column("skills_root_skill", "row")
