"""Keep the last confirmed contract mode for safe continuation during outages."""

from alembic import op

import sqlalchemy as sa

revision = "dailypolicy001"
down_revision = "dailystarts001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skills_lesson_starts", sa.Column("policy_mode", sa.String(16), nullable=True))
    # Only this old reason proves enforced Backend daily policy. Premium,
    # historical, off and shadow reasons do not establish a contract mode.
    table = sa.table("skills_lesson_starts", sa.column("policy_mode"), sa.column("reason"))
    op.execute(table.update().where(table.c.reason == "daily").values(policy_mode="daily"))


def downgrade() -> None:
    raise RuntimeError("Confirmed learning policy preserves access; use a reviewed forward migration")
