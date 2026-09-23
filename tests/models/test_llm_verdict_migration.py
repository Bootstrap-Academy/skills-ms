"""The grading-verdict migration is additive, the single head, and matches the runtime table."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from sqlalchemy import create_engine, inspect, text

from api.models import LlmVerdict
from api.utils.utc import utcnow


ROOT = Path(__file__).parents[2]


def test_llm_verdict_migration_is_additive_head_and_matches_model() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    assert ScriptDirectory.from_config(config).get_heads() == ["llmverdicts001"]
    path = ROOT / "alembic/versions/2026_09_23_2300-llmverdicts001_graded_completion.py"
    spec = spec_from_file_location("llm_verdict_migration", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "lessonmodules001"
    engine = create_engine("sqlite://", future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE legacy_evidence (value INTEGER NOT NULL)"))
            connection.execute(text("INSERT INTO legacy_evidence VALUES (23)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            assert connection.execute(text("SELECT value FROM legacy_evidence")).scalar_one() == 23
            columns = inspect(connection).get_columns(LlmVerdict.__tablename__)
            assert {column["name"]: column["nullable"] for column in columns} == {
                column.name: column.nullable for column in LlmVerdict.__table__.columns
            }
            primary = inspect(connection).get_pk_constraint(LlmVerdict.__tablename__)["constrained_columns"]
            assert primary == ["user_id", "request_id"]
            connection.execute(
                LlmVerdict.__table__.insert().values(
                    user_id="synthetic",
                    request_id="request",
                    unit_id="graded",
                    profile="grader",
                    profile_sha256="a" * 64,
                    answer_sha256="b" * 64,
                    score=3,
                    max_score=4,
                    model="gpt-6-sol",
                    graded_at=utcnow(),
                    used_at=utcnow(),
                )
            )
            assert connection.execute(text("SELECT count(*) FROM skills_llm_verdicts")).scalar_one() == 1
    finally:
        engine.dispose()
