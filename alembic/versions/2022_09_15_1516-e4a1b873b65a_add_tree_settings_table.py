"""add tree_settings table

Revision ID: e4a1b873b65a
Create Date: 2022-09-15 15:16:10.100583
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e4a1b873b65a"
down_revision = "10d85d8640cc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_tree_settings",
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("columns", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("rows", "columns"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("skills_tree_settings")
