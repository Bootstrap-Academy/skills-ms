"""Let a task state keep any request id of the lesson protocol (up to 100 characters); no data changes."""

from alembic import op

import sqlalchemy as sa

revision = "engine002"
down_revision = "engine001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite enforces no length; on PostgreSQL widening a VARCHAR rewrites nothing.
    if op.get_bind().dialect.name == "sqlite":
        return
    op.alter_column(
        "skills_engine_task_states",
        "start_request_id",
        type_=sa.String(100),
        existing_type=sa.String(36),
        existing_nullable=True,
    )


def downgrade() -> None:
    raise RuntimeError("Narrowing request ids requires a reviewed forward migration")
