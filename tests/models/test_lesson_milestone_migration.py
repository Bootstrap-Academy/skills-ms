"""The lesson-milestone outbox migration is additive, the single head, and matches the runtime table."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from sqlalchemy import create_engine, inspect, text

from api.models import LessonMilestoneDelivery
from api.utils.utc import utcnow


ROOT = Path(__file__).parents[2]


def test_lesson_milestone_migration_is_additive_head_and_matches_model() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["milestones001"]
    chain = [revision.revision for revision in script.walk_revisions()][:4]
    assert chain == ["milestones001", "courseproject001", "llmverdicts001", "lessonmodules001"]
    path = ROOT / "alembic/versions/2026_09_24_1200-milestones001_lesson_milestone_outbox.py"
    spec = spec_from_file_location("lesson_milestone_migration", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "courseproject001"
    engine = create_engine("sqlite://", future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE legacy_evidence (value INTEGER NOT NULL)"))
            connection.execute(text("INSERT INTO legacy_evidence VALUES (2)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            assert connection.execute(text("SELECT value FROM legacy_evidence")).scalar_one() == 2
            table = LessonMilestoneDelivery.__tablename__
            columns = inspect(connection).get_columns(table)
            assert {column["name"]: column["nullable"] for column in columns} == {
                column.name: column.nullable for column in LessonMilestoneDelivery.__table__.columns
            }
            assert inspect(connection).get_pk_constraint(table)["constrained_columns"] == ["user_id", "unit_id"]
            assert [index["column_names"] for index in inspect(connection).get_indexes(table)] == [["state"]]
            connection.execute(
                LessonMilestoneDelivery.__table__.insert().values(
                    user_id="synthetic",
                    unit_id="graded",
                    skill_id="prompting_basics",
                    xp=20,
                    completion="llm_verdict",
                    state="pending",
                    attempts=0,
                    next_attempt_at=utcnow(),
                    created_at=utcnow(),
                )
            )
            assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 1  # noqa: S608
    finally:
        engine.dispose()
