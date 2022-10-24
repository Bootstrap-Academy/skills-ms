"""add last_watch

Revision ID: f450fab91cb3
Create Date: 2022-10-24 11:33:32.454905
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f450fab91cb3"
down_revision = "efd84fdce9fc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_last_watch",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("course_id", sa.String(length=256), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("user_id", "course_id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("skills_last_watch")
