"""add sub skill tables

Revision ID: 67d6633de603
Create Date: 2022-09-14 18:50:17.769854
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "67d6633de603"
down_revision = "6e084001daeb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_sub_skill_dependency",
        sa.Column("dependent_id", sa.String(length=256), nullable=False),
        sa.Column("dependency_id", sa.String(length=256), nullable=False),
        sa.ForeignKeyConstraint(["dependency_id"], ["skills_sub_skill.id"]),
        sa.ForeignKeyConstraint(["dependent_id"], ["skills_sub_skill.id"]),
        sa.PrimaryKeyConstraint("dependent_id", "dependency_id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("skills_sub_skill_dependency")
