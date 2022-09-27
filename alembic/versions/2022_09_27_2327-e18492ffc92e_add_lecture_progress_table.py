"""add lecture_progress table

Revision ID: e18492ffc92e
Create Date: 2022-09-27 23:27:21.798006
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e18492ffc92e"
down_revision = "c53c21797f92"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_lecture_progress",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("course_id", sa.String(length=256), nullable=False),
        sa.Column("lecture_id", sa.String(length=256), nullable=False),
        sa.Column("completed", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("user_id", "course_id", "lecture_id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("skills_lecture_progress")
