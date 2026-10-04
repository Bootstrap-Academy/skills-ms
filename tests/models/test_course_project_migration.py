"""The course-project migration is additive, the single head, and matches the runtime tables."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from sqlalchemy import create_engine, inspect, text

from api.models import CourseProject, CourseProjectRequest
from api.utils.utc import utcnow

ROOT = Path(__file__).parents[2]


def test_course_project_migration_is_additive_head_and_matches_models() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    # Later migrations build on it; the current head is checked with the newest migration.
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert "courseproject001" in {revision.revision for revision in script.walk_revisions()}
    path = ROOT / "alembic/versions/2026_09_24_0900-courseproject001_course_project_state.py"
    spec = spec_from_file_location("course_project_migration", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "llmverdicts001"
    engine = create_engine("sqlite://", future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE legacy_evidence (value INTEGER NOT NULL)"))
            connection.execute(text("INSERT INTO legacy_evidence VALUES (24)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            assert connection.execute(text("SELECT value FROM legacy_evidence")).scalar_one() == 24
            tables: list[tuple[Any, list[str]]] = [
                (CourseProject, ["user_id", "course_id"]),
                (CourseProjectRequest, ["user_id", "request_id"]),
            ]
            for model, primary in tables:
                columns = inspect(connection).get_columns(model.__tablename__)
                assert {column["name"]: column["nullable"] for column in columns} == {
                    column.name: column.nullable for column in model.__table__.columns
                }
                assert inspect(connection).get_pk_constraint(model.__tablename__)["constrained_columns"] == primary
            connection.execute(
                CourseProject.__table__.insert().values(
                    user_id="synthetic", course_id="course", revision=1, state={"bot": {}}, updated_at=utcnow()
                )
            )
            connection.execute(
                CourseProjectRequest.__table__.insert().values(
                    user_id="synthetic",
                    request_id="request",
                    course_id="course",
                    revision=1,
                    fingerprint="f" * 64,
                    updated_at=utcnow(),
                )
            )
            assert connection.execute(text("SELECT count(*) FROM skills_course_projects")).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM skills_course_project_requests")).scalar_one() == 1
    finally:
        engine.dispose()
