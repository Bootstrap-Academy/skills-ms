"""Add private course-wide project state and its retry receipts; no data changes."""

from alembic import op

import sqlalchemy as sa


revision = "courseproject001"
down_revision = "llmverdicts001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_course_projects",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("course_id", sa.String(256), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "skills_course_project_requests",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("course_id", sa.String(256), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    raise RuntimeError("Removing private project state requires a reviewed forward migration")
