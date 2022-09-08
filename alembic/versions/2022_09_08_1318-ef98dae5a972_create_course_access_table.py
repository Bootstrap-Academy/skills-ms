"""create course_access table

Revision ID: ef98dae5a972
Create Date: 2022-09-08 13:18:15.538110
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "ef98dae5a972"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_course_access",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("course_id", sa.String(length=256), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "course_id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("skills_course_access")
