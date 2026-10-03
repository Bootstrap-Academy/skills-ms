"""Keep repeat practice separate from the original learning achievement."""

from alembic import op

import sqlalchemy as sa


revision = "learningreviews001"
down_revision = "learningrooms001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skills_room_states", sa.Column("review_id", sa.String(36), nullable=True))
    op.add_column("skills_room_states", sa.Column("review_status", sa.String(16), nullable=True))
    op.add_column("skills_room_states", sa.Column("review_started_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Removing private review state requires a reviewed forward migration")
