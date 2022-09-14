"""add skill_course table

Revision ID: a7c2f8ffc641
Create Date: 2022-09-14 21:13:08.559502
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a7c2f8ffc641"
down_revision = "67d6633de603"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills_skill_course",
        sa.Column("root_skill_id", sa.String(length=256), nullable=False),
        sa.Column("sub_skill_id", sa.String(length=256), nullable=False),
        sa.Column("course_id", sa.String(length=256), nullable=False),
        sa.ForeignKeyConstraint(
            ["root_skill_id", "sub_skill_id"], ["skills_sub_skill.parent_id", "skills_sub_skill.id"]
        ),
        sa.PrimaryKeyConstraint("root_skill_id", "sub_skill_id", "course_id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("skills_skill_course")
