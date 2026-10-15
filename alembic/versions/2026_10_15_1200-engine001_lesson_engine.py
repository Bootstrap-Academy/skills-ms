"""Add the lesson engine tables: package revisions, task states and submissions; no data changes."""

from alembic import op

import sqlalchemy as sa

revision = "engine001"
down_revision = "platformbasis001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_engine_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("unit_id", sa.String(80), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("active_for", sa.String(80), nullable=True),
        sa.Column("module_id", sa.String(128), sa.ForeignKey("skills_lesson_modules.id"), nullable=False),
        sa.Column("package_hash", sa.String(64), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("entry_path", sa.String(512), nullable=False),
        sa.Column("server_document", sa.JSON(), nullable=False),
        sa.Column("server_sha256", sa.String(64), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("tasks", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("unit_id", "number", name="uq_engine_revision_number"),
        sa.UniqueConstraint("unit_id", "server_sha256", "package_hash", name="uq_engine_revision_content"),
        sa.UniqueConstraint("active_for", name="uq_engine_revision_active"),
        sa.CheckConstraint("active_for IS NULL OR active_for = unit_id", name="ck_engine_revision_active_for"),
        sa.CheckConstraint("number >= 1", name="ck_engine_revision_number"),
        sa.CheckConstraint(
            "length(package_hash) = 64 AND length(manifest_hash) = 64 AND length(server_sha256) = 64 "
            "AND length(evidence_sha256) = 64",
            name="ck_engine_revision_hashes",
        ),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "skills_engine_task_states",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("unit_id", sa.String(80), primary_key=True),
        sa.Column("goal_id", sa.String(80), primary_key=True),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("skills_engine_revisions.id"), nullable=False),
        sa.Column("band", sa.String(64), nullable=False),
        sa.Column("fails", sa.Integer(), nullable=False),
        sa.Column("passes", sa.Integer(), nullable=False),
        sa.Column("start_id", sa.String(36), nullable=True),
        sa.Column("start", sa.JSON(), nullable=True),
        sa.Column("start_band", sa.String(64), nullable=True),
        sa.Column("start_request_id", sa.String(36), nullable=True),
        sa.Column("review_id", sa.String(36), nullable=True),
        sa.Column("passed_at", sa.DateTime(), nullable=True),
        sa.Column("xp_applied", sa.Boolean(), nullable=False),
        sa.Column("xp_operation_id", sa.String(36), nullable=True),
        sa.Column("xp_amount", sa.Integer(), nullable=True),
        sa.Column("xp_skill_id", sa.String(256), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("fails >= 0 AND passes >= 0", name="ck_engine_task_counters"),
        sa.CheckConstraint("xp_amount IS NULL OR xp_amount >= 0", name="ck_engine_task_xp_amount"),
        sa.CheckConstraint(
            "(start_id IS NULL AND start IS NULL AND start_band IS NULL) "
            "OR (start_id IS NOT NULL AND start IS NOT NULL AND start_band IS NOT NULL)",
            name="ck_engine_task_start",
        ),
        sa.CheckConstraint(
            "NOT xp_applied OR (xp_operation_id IS NOT NULL AND xp_amount IS NOT NULL AND xp_skill_id IS NOT NULL "
            "AND passed_at IS NOT NULL)",
            name="ck_engine_task_xp",
        ),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "skills_engine_submissions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("unit_id", sa.String(80), nullable=False),
        sa.Column("goal_id", sa.String(80), nullable=False),
        sa.Column("open_goal_id", sa.String(80), nullable=True),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("skills_engine_revisions.id"), nullable=False),
        sa.Column("start_id", sa.String(36), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("job", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=True),
        sa.Column("feedback", sa.JSON(), nullable=True),
        sa.Column("counts", sa.Boolean(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(), nullable=False),
        sa.Column("booked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id", "unit_id", "goal_id"],
            [
                "skills_engine_task_states.user_id",
                "skills_engine_task_states.unit_id",
                "skills_engine_task_states.goal_id",
            ],
            name="fk_engine_submission_task",
        ),
        sa.UniqueConstraint(
            "user_id", "unit_id", "goal_id", "start_id", "input_hash", name="uq_engine_submission_plan"
        ),
        sa.UniqueConstraint("user_id", "unit_id", "open_goal_id", name="uq_engine_submission_open"),
        sa.CheckConstraint("state IN ('accepted', 'booked')", name="ck_engine_submission_state"),
        sa.CheckConstraint(
            "(state = 'accepted' AND open_goal_id = goal_id AND verdict IS NULL AND booked_at IS NULL) "
            "OR (state = 'booked' AND open_goal_id IS NULL AND verdict IS NOT NULL AND booked_at IS NOT NULL "
            "AND counts IS NOT NULL)",
            name="ck_engine_submission_booking",
        ),
        sa.CheckConstraint(
            "verdict IS NULL OR verdict IN ('passed', 'not_passed')", name="ck_engine_submission_verdict"
        ),
        sa.CheckConstraint("length(input_hash) = 64", name="ck_engine_submission_input_hash"),
        mysql_collate="utf8mb4_bin",
    )
    op.create_index("ix_engine_submissions_user", "skills_engine_submissions", ["user_id"])


def downgrade() -> None:
    raise RuntimeError("Removing learner submissions and verdicts requires a reviewed forward migration")
