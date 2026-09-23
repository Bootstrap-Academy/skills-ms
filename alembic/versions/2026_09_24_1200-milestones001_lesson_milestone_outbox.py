"""Add the outbox of lesson milestones (XP-02) for challenges-ms; no data changes."""

from alembic import op

import sqlalchemy as sa


revision = "milestones001"
down_revision = "courseproject001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_lesson_milestones",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("unit_id", sa.String(80), primary_key=True),
        sa.Column("skill_id", sa.String(256), nullable=False),
        sa.Column("xp", sa.Integer(), nullable=False),
        sa.Column("completion", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("last_status", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        mysql_collate="utf8mb4_bin",
    )
    op.create_index("ix_skills_lesson_milestones_state", "skills_lesson_milestones", ["state"])


def downgrade() -> None:
    raise RuntimeError("Removing undelivered lesson milestones requires a reviewed forward migration")
