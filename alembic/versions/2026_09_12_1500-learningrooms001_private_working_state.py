"""Add private learning-room state and retry receipts without legacy data changes."""

from alembic import op

import sqlalchemy as sa


revision = "learningrooms001"
down_revision = "l3xp001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_room_states",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("unit_id", sa.String(80), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "skills_room_requests",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("unit_id", sa.String(80), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    raise RuntimeError("Removing private learning state requires a reviewed forward migration")
