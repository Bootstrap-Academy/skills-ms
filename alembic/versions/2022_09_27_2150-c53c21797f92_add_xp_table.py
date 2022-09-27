"""add xp table

Revision ID: c53c21797f92
Create Date: 2022-09-27 21:50:36.885690
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c53c21797f92"
down_revision = "522023e445a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_xp",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("skill_id", sa.String(length=256), nullable=True),
        sa.Column("xp", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["skill_id"], ["skills_sub_skill.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("skills_xp")
