"""Add the trusted lesson-module registry; preserve all content and learner rows."""

from alembic import op

import sqlalchemy as sa


revision = "lessonmodules001"
down_revision = "learningreviews001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_lesson_modules",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("api_version", sa.Integer(), nullable=False),
        sa.Column("entry_url", sa.String(2048), nullable=False),
        sa.CheckConstraint("api_version = 1", name="ck_lesson_module_api_version"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    raise RuntimeError("Removing published module references requires a reviewed forward migration")
