"""Join the additive learning-access and private-project migration branches."""

revision = "platformbasis001"
down_revision = ("dailypolicy001", "milestones001")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Both branches already own their tables. Joining them changes no learner data.
    pass


def downgrade() -> None:
    raise RuntimeError("Splitting the platform schema requires a reviewed forward migration")
