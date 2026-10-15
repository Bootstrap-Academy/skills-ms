"""The lesson engine migration is additive, the single head, matches the models, and its constraints hold.

The same constraints are checked on PostgreSQL by `tests/engine_postgres.py`.
"""

from collections.abc import Iterator
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from api.models import EngineRevision, EngineSubmission, EngineTaskState
from tests.engine_rows import VIOLATIONS, revision, submission, task_state

ROOT = Path(__file__).parents[2]
TABLES: tuple[Any, ...] = (EngineRevision, EngineTaskState, EngineSubmission)


def migration() -> Any:
    path = ROOT / "alembic/versions/2026_10_15_1200-engine001_lesson_engine.py"
    spec = spec_from_file_location("engine_migration", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def connection() -> Iterator[Connection]:
    engine = create_engine("sqlite://", future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("CREATE TABLE legacy_evidence (value INTEGER NOT NULL)"))
            connection.execute(text("INSERT INTO legacy_evidence VALUES (23)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration().upgrade()
            connection.commit()
            yield connection
    finally:
        engine.dispose()


def row(model: Any) -> dict[str, Any]:
    return {column.name: getattr(model, column.name) for column in model.__table__.columns}


def insert(connection: Connection, *models: Any) -> None:
    for model in models:
        connection.execute(model.__table__.insert().values(**row(model)))


def test_engine_migration_is_additive_head_and_matches_models(connection: Connection) -> None:
    scripts = ScriptDirectory(str(ROOT / "alembic"))
    assert len(scripts.get_heads()) == 1
    assert "engine001" in {revision.revision for revision in scripts.walk_revisions()}
    assert migration().down_revision == "platformbasis001"
    assert connection.execute(text("SELECT value FROM legacy_evidence")).scalar_one() == 23
    for model in TABLES:
        columns = inspect(connection).get_columns(model.__tablename__)
        assert {column["name"]: column["nullable"] for column in columns} == {
            column.name: column.nullable for column in model.__table__.columns
        }
    assert inspect(connection).get_pk_constraint(EngineTaskState.__tablename__)["constrained_columns"] == [
        "user_id",
        "unit_id",
        "goal_id",
    ]
    # Every named constraint of the models exists in the migrated schema, and the other way round.
    for model in TABLES:
        table = model.__tablename__
        migrated = {item["name"] for item in inspect(connection).get_check_constraints(table)}
        migrated |= {item["name"] for item in inspect(connection).get_unique_constraints(table)}
        declared = {constraint.name for constraint in model.__table__.constraints if constraint.name}
        declared -= {item["name"] for item in inspect(connection).get_foreign_keys(table)}
        assert declared == migrated, table


def test_valid_rows_are_accepted(connection: Connection) -> None:
    insert(
        connection,
        revision(),
        revision(number=2, active=False),
        revision(number=3, active=False),
        revision("other-unit"),
        task_state("learner"),
        task_state("learner", goal_id="second"),
        submission("learner", "s-1", input_hash="1" * 64),
        submission("learner", "s-2", input_hash="2" * 64),
        submission("learner", "s-3", input_hash="3" * 64, booked=False),
        submission("learner", "s-4", goal_id="second", booked=False),
    )
    assert connection.execute(text("SELECT count(*) FROM skills_engine_submissions")).scalar_one() == 4


@pytest.mark.parametrize("name", VIOLATIONS)
def test_the_schema_refuses(connection: Connection, name: str) -> None:
    rows = VIOLATIONS[name]()
    *valid, refused = rows
    insert(connection, *valid)
    with pytest.raises(IntegrityError):
        insert(connection, refused)
