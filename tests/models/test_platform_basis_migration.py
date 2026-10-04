"""Both deployed schema branches join without losing existing or branch-owned data."""

from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection

ROOT = Path(__file__).parents[2]
HEAD = "platformbasis001"


def upgrade(connection: Connection, scripts: ScriptDirectory, target: str) -> None:
    context = MigrationContext.configure(
        connection,
        opts={
            "version_table": "skills_alembic_version",
            "fn": lambda current, context: scripts._upgrade_revs(target, current),
        },
    )
    with Operations.context(context):
        context.run_migrations()


@pytest.mark.parametrize("predecessor", ["lessonmodules001", "dailypolicy001", "milestones001", "both"])
def test_forward_merge_preserves_predecessor_data(predecessor: str) -> None:
    scripts = ScriptDirectory(str(ROOT / "alembic"))
    assert scripts.get_heads() == [HEAD]
    merged = scripts.get_revision(HEAD)
    assert merged is not None and merged.down_revision == ("dailypolicy001", "milestones001")
    engine = create_engine("sqlite://", future=True)
    try:
        with engine.begin() as connection:
            # Earlier schema/data are represented here; full fresh PostgreSQL migrations
            # and real rights/drafts/XP are checked by platform-acceptance.
            connection.execute(text("CREATE TABLE existing_learner_data (kind TEXT PRIMARY KEY, value TEXT NOT NULL)"))
            for kind in ("purchased_right", "draft", "xp"):
                connection.execute(
                    text("INSERT INTO existing_learner_data VALUES (:kind, :value)"),
                    {"kind": kind, "value": "original " + kind},
                )
            MigrationContext.configure(connection, opts={"version_table": "skills_alembic_version"}).stamp(
                scripts, "lessonmodules001"
            )
            if predecessor == "both":
                upgrade(connection, scripts, "dailypolicy001")
                upgrade(connection, scripts, "milestones001")
            elif predecessor != "lessonmodules001":
                upgrade(connection, scripts, predecessor)

            # Keep a real row in each table that already exists on this branch.
            tables = set(inspect(connection).get_table_names())
            if "skills_course_projects" in tables:
                connection.execute(
                    text(
                        "INSERT INTO skills_course_projects VALUES "
                        "('synthetic','course',3,'{\"draft\":\"kept\"}','2026-10-03 12:00:00')"
                    )
                )
            if "skills_lesson_milestones" in tables:
                connection.execute(
                    text(
                        "INSERT INTO skills_lesson_milestones "
                        "(user_id,unit_id,skill_id,xp,completion,state,attempts,next_attempt_at,created_at) VALUES "
                        "('synthetic','unit','subskill',10,'deterministic','pending',1,"
                        "'2026-10-03 12:00:00','2026-10-03 12:00:00')"
                    )
                )
            before = {
                table: connection.execute(text(f'SELECT * FROM "{table}"')).all()  # noqa: S608
                for table in tables
                if table != "skills_alembic_version"
            }
            upgrade(connection, scripts, "head")
            assert connection.execute(text("SELECT version_num FROM skills_alembic_version")).all() == [(HEAD,)]
            for table, rows in before.items():
                assert connection.execute(text(f'SELECT * FROM "{table}"')).all() == rows  # noqa: S608
            assert {
                "skills_lesson_starts",
                "skills_lesson_start_requests",
                "skills_daily_limit_settings",
                "skills_llm_verdicts",
                "skills_course_projects",
                "skills_course_project_requests",
                "skills_lesson_milestones",
            }.issubset(inspect(connection).get_table_names())
            # Repeating the normal startup migration is harmless.
            upgrade(connection, scripts, "head")
            assert connection.execute(text("SELECT version_num FROM skills_alembic_version")).scalar_one() == HEAD
    finally:
        engine.dispose()
