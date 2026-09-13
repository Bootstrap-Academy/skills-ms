"""The additive migration matches runtime tables and preserves unrelated rows."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations

from sqlalchemy import create_engine, inspect, text

from api.models.room import RoomRequest, RoomState
from api.utils.utc import utcnow


def test_room_migration_is_additive_and_matches_models() -> None:
    path = Path(__file__).parents[2] / "alembic/versions/2026_09_12_1500-learningrooms001_private_working_state.py"
    spec = spec_from_file_location("room_migration", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "l3xp001"
    review_path = path.with_name("2026_09_12_1800-learningreviews001_private_review_state.py")
    review_spec = spec_from_file_location("review_migration", review_path)
    assert review_spec is not None and review_spec.loader is not None
    review_migration = module_from_spec(review_spec)
    review_spec.loader.exec_module(review_migration)
    assert review_migration.down_revision == migration.revision
    engine = create_engine("sqlite://", future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE legacy_evidence (value INTEGER NOT NULL)"))
            connection.execute(text("INSERT INTO legacy_evidence VALUES (17)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            connection.execute(
                RoomState.__table__.insert().values(
                    user_id="synthetic",
                    unit_id="intro",
                    revision=1,
                    state={"draft": "Übung"},
                    status="in_progress",
                    result=None,
                    updated_at=utcnow(),
                )
            )
            with Operations.context(MigrationContext.configure(connection)):
                review_migration.upgrade()
            assert connection.execute(text("SELECT value FROM legacy_evidence")).scalar_one() == 17
            for table in (RoomState.__table__, RoomRequest.__table__):
                columns = inspect(connection).get_columns(table.name)
                assert {column["name"]: column["nullable"] for column in columns} == {
                    column.name: column.nullable for column in table.columns
                }
            row = connection.execute(RoomState.__table__.select()).one()
            assert row.state == {"draft": "Übung"} and row.revision == 1 and row.status == "in_progress"
            assert row.review_id is row.review_status is row.review_started_at is None
    finally:
        engine.dispose()
