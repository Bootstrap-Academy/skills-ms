"""Add single-use grading verdict receipts for LLM-graded room completions; no data changes."""

from alembic import op

import sqlalchemy as sa


revision = "llmverdicts001"
down_revision = "lessonmodules001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_llm_verdicts",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("unit_id", sa.String(80), nullable=False),
        sa.Column("profile", sa.String(80), nullable=False),
        sa.Column("profile_sha256", sa.String(64), nullable=False),
        sa.Column("answer_sha256", sa.String(64), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("max_score", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("graded_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=False),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    raise RuntimeError("Removing grading evidence requires a reviewed forward migration")
