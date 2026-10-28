"""Add the learner's own learning plan, one row per learner; no data changes."""

from alembic import op

import sqlalchemy as sa

revision = "plan001"
down_revision = "engine002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_learning_plans",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    raise RuntimeError("Removing learning plans requires a reviewed forward migration")
