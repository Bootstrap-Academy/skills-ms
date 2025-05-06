"""empty message

Revision ID: 4f7559aa1483
Create Date: 2024-07-06 20:09:48.351518
"""

from alembic import op

import sqlalchemy as sa


revision = "4f7559aa1483"
down_revision = "1a9e957978e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_sub_skill_bookmark",
        sa.Column("bookmark_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("root_skill_id", sa.String(length=256), nullable=True),
        sa.Column("sub_skill_id", sa.String(length=256), nullable=True),
        sa.PrimaryKeyConstraint("bookmark_id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("skills_sub_skill_bookmark")
