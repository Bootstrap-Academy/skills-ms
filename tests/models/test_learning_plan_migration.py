"""The learning-plan migration is additive, the single head, and matches the runtime table."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.models import LearningPlan
from api.utils.utc import utcnow

ROOT = Path(__file__).parents[2]


def test_learning_plan_migration_is_additive_head_and_matches_the_model() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["plan001"]
    path = ROOT / "alembic/versions/2026_10_28_1200-plan001_learning_plan.py"
    spec = spec_from_file_location("learning_plan_migration", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert (migration.revision, migration.down_revision) == ("plan001", "engine002")
    with pytest.raises(RuntimeError):
        migration.downgrade()
    engine = create_engine("sqlite://", future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE legacy_evidence (value INTEGER NOT NULL)"))
            connection.execute(text("INSERT INTO legacy_evidence VALUES (28)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            assert connection.execute(text("SELECT value FROM legacy_evidence")).scalar_one() == 28
            columns = inspect(connection).get_columns(LearningPlan.__tablename__)
            assert {column["name"]: column["nullable"] for column in columns} == {
                column.name: column.nullable for column in LearningPlan.__table__.columns
            }
            assert inspect(connection).get_pk_constraint(LearningPlan.__tablename__)["constrained_columns"] == [
                "user_id"
            ]
            row = {
                "user_id": "synthetic",
                "revision": 1,
                "state": {"steps": [{"type": "course", "course": "linux"}]},
                "request_id": "0b6c9a62-55c7-4bd4-9f1e-7d3b8c2a1e40",
                "request_fingerprint": "f" * 64,
                "updated_at": utcnow(),
            }
            connection.execute(LearningPlan.__table__.insert().values(**row))
            # One plan per learner.
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(LearningPlan.__table__.insert().values(**row))
            assert connection.execute(text("SELECT count(*) FROM skills_learning_plans")).scalar_one() == 1
    finally:
        engine.dispose()
