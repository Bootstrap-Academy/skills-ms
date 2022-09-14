"""add root skill tables

Revision ID: 6e084001daeb
Create Date: 2022-09-14 16:59:20.592312
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "6e084001daeb"
down_revision = "ef98dae5a972"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_root_skill",
        sa.Column("id", sa.String(length=256), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "skills_root_skill_dependency",
        sa.Column("dependent_id", sa.String(length=256), nullable=False),
        sa.Column("dependency_id", sa.String(length=256), nullable=False),
        sa.ForeignKeyConstraint(["dependency_id"], ["skills_root_skill.id"]),
        sa.ForeignKeyConstraint(["dependent_id"], ["skills_root_skill.id"]),
        sa.PrimaryKeyConstraint("dependent_id", "dependency_id"),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "skills_sub_skill",
        sa.Column("id", sa.String(length=256), nullable=False),
        sa.Column("parent_id", sa.String(length=256), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=True),
        sa.ForeignKeyConstraint(["parent_id"], ["skills_root_skill.id"]),
        sa.PrimaryKeyConstraint("id", "parent_id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("skills_sub_skill")
    op.drop_table("skills_root_skill_dependency")
    op.drop_table("skills_root_skill")
