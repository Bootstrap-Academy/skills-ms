"""Prospective exact XP receipts; no inference about historic awards."""
from alembic import op
import sqlalchemy as sa

revision = "l3xp001"
down_revision = "l3courseright001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_xp_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        mysql_collate="utf8mb4_bin",
    )
    op.create_index("ix_skills_xp_operations_user_id", "skills_xp_operations", ["user_id"])


def downgrade() -> None:
    raise RuntimeError("Benefit receipts require a reviewed forward migration")
