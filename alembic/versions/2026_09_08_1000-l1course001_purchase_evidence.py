"""Durable course purchase commands; no historical acceptance is inferred."""

from alembic import op

import sqlalchemy as sa


revision = "l1course001"
down_revision = "4f7559aa1483"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_purchase_users",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("deleted", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "skills_course_purchases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("course_id", sa.String(256), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("active_key", sa.String(512), nullable=True, unique=True),
        sa.Column("offer", sa.JSON(), nullable=False),
        sa.Column("fulfillment", sa.JSON(), nullable=True),
        sa.Column("reported", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("acceptance", sa.JSON()),
        sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_skills_course_purchases_user_id", "skills_course_purchases", ["user_id"])
    op.create_index("ix_skills_course_purchases_state", "skills_course_purchases", ["state"])


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM skills_course_purchases WHERE acceptance IS NOT NULL"))
        .scalar()
    ):
        raise RuntimeError("Archive accepted purchase evidence before a deliberate downgrade")
    op.drop_table("skills_course_purchases")
    op.drop_table("skills_purchase_users")
