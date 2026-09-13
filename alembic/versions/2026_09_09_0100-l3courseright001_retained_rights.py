"""Prospectively preserve actual course-access observations before erasure."""

from alembic import op

import sqlalchemy as sa


revision = "l3courseright001"
down_revision = "l1course001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_retained_course_rights",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_user_id", sa.String(36), nullable=False),
        sa.Column("course_id", sa.String(256), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("original", sa.JSON(), nullable=False),
        sa.Column("current_subject", sa.String(36), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("source_user_id", "course_id"),
        mysql_collate="utf8mb4_bin",
    )
    op.create_index(
        "ix_skills_retained_course_rights_source_user_id", "skills_retained_course_rights", ["source_user_id"]
    )
    op.create_index(
        "ix_skills_retained_course_rights_current_subject", "skills_retained_course_rights", ["current_subject"]
    )
    op.create_table(
        "skills_course_right_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("right_id", sa.String(36), sa.ForeignKey("skills_retained_course_rights.id"), nullable=False),
        sa.Column("subject", sa.String(36), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        mysql_collate="utf8mb4_bin",
    )
    op.create_index("ix_skills_course_right_grants_subject", "skills_course_right_grants", ["subject"])


def downgrade() -> None:
    raise RuntimeError("Existing course-right observations and grants require a reviewed forward migration")
