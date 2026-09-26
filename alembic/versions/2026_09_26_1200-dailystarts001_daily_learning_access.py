"""Add configurable daily lesson admission; disabled until explicitly configured."""

from datetime import datetime

from alembic import op

import sqlalchemy as sa


revision = "dailystarts001"
down_revision = "lessonmodules001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_lesson_starts",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("course_id", sa.String(256), primary_key=True),
        sa.Column("lesson_id", sa.String(256), primary_key=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("local_day", sa.Date(), nullable=False),
        sa.Column("charged", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        mysql_collate="utf8mb4_bin",
    )
    op.create_index("ix_lesson_starts_day", "skills_lesson_starts", ["user_id", "local_day", "charged"])
    op.create_table(
        "skills_lesson_start_requests",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("course_id", sa.String(256), nullable=False),
        sa.Column("lesson_id", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        mysql_collate="utf8mb4_bin",
    )
    table = op.create_table(
        "skills_daily_limit_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("lesson_limit", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.String(100), nullable=False),
        sa.Column("note", sa.String(512), nullable=False),
        sa.CheckConstraint("mode IN ('off', 'shadow', 'enforce')", name="ck_daily_limit_mode"),
        sa.CheckConstraint("lesson_limit BETWEEN 1 AND 100", name="ck_daily_limit_value"),
        mysql_collate="utf8mb4_bin",
    )
    op.bulk_insert(
        table,
        [
            {
                "id": 1,
                "mode": "off",
                "lesson_limit": 3,
                "updated_at": datetime.utcnow(),
                "updated_by": "migration",
                "note": "Disabled; no public activation authorized.",
            }
        ],
    )


def downgrade() -> None:
    raise RuntimeError("Lesson starts preserve access; use a reviewed forward migration")
