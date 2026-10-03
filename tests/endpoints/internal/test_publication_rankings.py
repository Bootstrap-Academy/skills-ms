"""SQL comparison sets and fresh publication fences (also run on real PG)."""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from _pytest.monkeypatch import MonkeyPatch
from fastapi import HTTPException, Response
from httpx import AsyncClient
from sqlalchemy import func
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.future import select
from sqlalchemy.sql import ClauseElement, Select
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.compiler import SQLCompiler

from api import models
from api.database import db, db_context
from api.endpoints.internal import skills
from api.services import publications
from api.settings import settings
from api.utils.jwt import encode_jwt


IDS = tuple(str(UUID(int=number)) for number in range(1, 7))


class Explain(Executable, ClauseElement):
    inherit_cache = False

    def __init__(self, query: Select) -> None:
        self.query = query


@compiles(Explain)  # type: ignore[misc]  # SQLAlchemy 1.4's compiler registry is untyped.
def compile_explain(element: Explain, compiler: SQLCompiler, **kwargs: Any) -> str:
    return "EXPLAIN (ANALYZE, FORMAT JSON) " + compiler.process(element.query, **kwargs)


@pytest.fixture(autouse=True)
async def ranking_database(database: None, monkeypatch: MonkeyPatch) -> AsyncIterator[None]:
    url = os.environ.get("PRIV01_TEST_DATABASE_URL")
    if not url:
        yield
        return
    assert url.startswith("postgresql+asyncpg://") and "@127.0.0.1:" in url and url.endswith("/priv01_rankings")
    engine = create_async_engine(url)
    monkeypatch.setattr(db, "engine", engine)
    await db.create_tables()
    try:
        yield
    finally:
        from api.database import Base

        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def seed() -> None:
    await db.add(models.SubSkill(id="one", name="One"))
    await db.add(models.SubSkill(id="two", name="Two"))
    for index, xp in enumerate((900, 30, 20, 20, 10, 0)):
        await db.add(
            models.XP(
                id=str(uuid4()),
                user_id=IDS[index],
                skill_id="one",
                xp=xp,
                last_update=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
    # Scores aggregate by user, never by number of skill records.
    await db.add(
        models.XP(
            id=str(uuid4()),
            user_id=IDS[0],
            skill_id="two",
            xp=500,
            last_update=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )


def snapshot(ids: tuple[str, ...] = IDS[1:]) -> publications.Snapshot:
    return publications.Snapshot(
        scope_version=publications.SCOPE_VERSION,
        publication_epoch=uuid4(),
        epoch_revision=1,
        policy_active=True,
        publishing_enabled=True,
        participants=tuple(
            publications.Participant(user_id=UUID(user), visibility_revision=1, display_name="Shared", avatar_url=None)
            for user in ids
        ),
    )


async def test_sql_filters_before_rank_count_and_pages() -> None:
    async with db_context():
        await seed()
        assert await models.XP.count_users() == 6
        assert await models.XP.rank_of(20) == 3
        assert await models.XP.count_users(IDS[1:]) == 5
        assert await models.XP.rank_of(20, IDS[1:]) == 2
        assert await models.XP.get_leaderboard(2, 0, IDS[1:]) == [(IDS[1], 30, 1), (IDS[2], 20, 2)]
        assert await models.XP.get_leaderboard(2, 2, IDS[1:]) == [(IDS[3], 20, 2), (IDS[4], 10, 4)]
        assert await models.XP.get_leaderboard(2, 4, IDS[1:]) == [(IDS[5], 0, 5)]
        assert await models.XP.get_leaderboard(2, 20, IDS[1:]) == []
        assert await models.XP.get_leaderboard(10, 0, ()) == []
        assert await models.XP.count_users(()) == 0
        # A private score update cannot move any public row, rank or count.
        before = await models.XP.get_leaderboard(10, 0, IDS[1:])
        record = await db.get(models.XP, user_id=IDS[0], skill_id="one")
        assert record is not None
        record.xp = 90000
        await db.session.flush()
        assert await models.XP.get_leaderboard(10, 0, IDS[1:]) == before
        assert await models.XP.count_users(IDS[1:]) == 5
        assert await models.XP.get_user_xp(IDS[0]) == 90500


async def test_revocation_during_calculation_reloads_before_output(monkeypatch: MonkeyPatch) -> None:
    old = snapshot()
    new = snapshot(IDS[2:])
    monkeypatch.setattr(publications, "current_snapshot", AsyncMock(side_effect=[old, new]))
    epoch = AsyncMock(side_effect=[new.epoch, new.epoch])
    monkeypatch.setattr(publications, "current_epoch", epoch)
    async with db_context():
        await seed()
        result = await skills.published_leaderboard(10, 0)
    assert result.publication_epoch == new.publication_epoch
    assert result.total == 4
    assert [row.user for row in result.leaderboard] == list(IDS[2:])
    assert [row.rank for row in result.leaderboard] == [1, 1, 3, 4]
    assert epoch.await_count == 2


async def test_private_own_score_has_no_public_rank(monkeypatch: MonkeyPatch) -> None:
    shared = snapshot()
    monkeypatch.setattr(publications, "current_snapshot", AsyncMock(return_value=shared))
    monkeypatch.setattr(publications, "current_epoch", AsyncMock(return_value=shared.epoch))
    async with db_context():
        await seed()
        result = await skills.published_rank(IDS[0])
    assert result.xp == 1400 and result.rank is None and result.public_rank is None


async def test_real_internal_routes_keep_metadata_and_require_service_auth(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    shared = snapshot()
    monkeypatch.setattr(settings, "profile_publications_enabled", True)
    monkeypatch.setattr(publications, "current_snapshot", AsyncMock(return_value=shared))
    monkeypatch.setattr(publications, "current_epoch", AsyncMock(return_value=shared.epoch))
    token = encode_jwt({"aud": "skills"}, timedelta(minutes=1), secret=settings.internal_jwt_secret("skills"))
    headers = {"Authorization": f"Bearer {token}", "If-None-Match": "*"}
    query = f"publication_epoch={shared.publication_epoch}&scope_version={publications.SCOPE_VERSION}"
    async with db_context():
        await seed()
        for route in [
            "/_internal/leaderboard?limit=2&offset=0",
            f"/_internal/published-leaderboard?limit=2&offset=0&{query}",
        ]:
            response = await client.get(route, headers=headers)
            assert response.status_code == 200
            assert response.json()["publication_epoch"] == str(shared.publication_epoch)
            assert response.json()["scope_version"] == publications.SCOPE_VERSION
            assert response.json()["total"] == 5
            assert response.headers["Cache-Control"] == "private, no-store"
            assert response.headers["Vary"] == "Authorization"
            assert (await client.get(route)).status_code == 401
        response = await client.get(f"/_internal/published-leaderboard/{IDS[0]}?{query}", headers=headers)
        assert response.status_code == 200 and response.json()["public_rank"] is None
        for route in [f"/_internal/leaderboard/{IDS[1]}", f"/_internal/published-leaderboard/{IDS[1]}?{query}"]:
            response = await client.get(route, headers=headers)
            assert response.status_code == 200
            assert response.json()["public_rank"] == 1
            assert response.json()["publication_epoch"] == str(shared.publication_epoch)


async def test_old_page_epoch_is_a_conflict(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(publications, "current_snapshot", AsyncMock(return_value=snapshot()))
    with pytest.raises(HTTPException) as error:
        await skills.published_leaderboard(10, 0, uuid4(), publications.SCOPE_VERSION)
    assert error.value.status_code == 409


@pytest.mark.parametrize(
    "enabled,active,publishing,expected",
    [
        (False, False, False, False),
        (True, True, True, True),
        (False, True, True, 503),
        (True, False, False, 503),
        (True, True, False, 503),
    ],
)
async def test_persistent_policy_fences_disabled_reader(
    monkeypatch: MonkeyPatch, enabled: bool, active: bool, publishing: bool, expected: bool | int
) -> None:
    shared = snapshot().epoch.copy(update={"policy_active": active, "publishing_enabled": publishing})
    monkeypatch.setattr(settings, "profile_publications_enabled", enabled)
    monkeypatch.setattr(publications, "current_epoch", AsyncMock(return_value=shared))
    if expected == 503:
        with pytest.raises(HTTPException) as error:
            await publications.use_shared_rankings()
        assert error.value.status_code == 503
    else:
        assert await publications.use_shared_rankings() is expected


async def test_legacy_wire_shape_and_scores_are_unchanged(monkeypatch: MonkeyPatch) -> None:
    legacy = snapshot().epoch.copy(update={"policy_active": False, "publishing_enabled": False})
    monkeypatch.setattr(publications, "current_epoch", AsyncMock(return_value=legacy))
    monkeypatch.setattr(settings, "profile_publications_enabled", False)
    async with db_context():
        await seed()
        result = await skills.get_leaderboard(2, 0, Response())
        assert result.dict() == {
            "leaderboard": [{"user": IDS[0], "xp": 1400, "rank": 1}, {"user": IDS[1], "xp": 30, "rank": 2}],
            "total": 6,
        }
        assert (await skills.get_leaderboard_user(IDS[0], Response())).dict() == {"xp": 1400, "rank": 1}


@pytest.mark.parametrize(
    "field", ["scope_version", "publication_epoch", "epoch_revision", "policy_active", "publishing_enabled"]
)
def test_old_authority_cannot_grant_publication(field: str) -> None:
    from pydantic import ValidationError

    payload = snapshot().dict()
    del payload[field]
    with pytest.raises(ValidationError):
        publications.Snapshot.parse_obj(payload)


def test_invalid_identity_is_not_a_publishable_participant() -> None:
    from pydantic import ValidationError

    payload = snapshot().dict()
    del payload["participants"][0]["avatar_url"]
    with pytest.raises(ValidationError):
        publications.Snapshot.parse_obj(payload)
    payload = snapshot().dict()
    payload["participants"] += (payload["participants"][0],)
    with pytest.raises(ValidationError):
        publications.Snapshot.parse_obj(payload)


async def test_large_snapshot_uses_one_postgres_array_bind() -> None:
    if db.engine.dialect.name != "postgresql":
        pytest.skip("Query plan requires the owned PostgreSQL run")
    participants = IDS[1:] + tuple(str(UUID(int=number)) for number in range(100, 33100))
    async with db_context():
        await seed()
        await db.session.flush()
        query = models.XP.published_participants(select(models.XP.user_id, func.sum(models.XP.xp)), participants)
        query = query.group_by(models.XP.user_id)
        compiled = query.compile(dialect=db.engine.dialect)
        assert len(compiled.params) == 1
        rows = await db.session.execute(Explain(query))
        plans = rows.scalar()
        assert plans is not None
        if isinstance(plans, str):
            plans = json.loads(plans)
        plan = plans[0]
        assert plan["Plan"]["Actual Rows"] == 5
        assert await models.XP.count_users(participants) == 5
        assert [rank for _, _, rank in await models.XP.get_leaderboard(100, 0, participants)] == [1, 2, 2, 4, 5]
        print(
            json.dumps(
                {
                    "participants": len(participants),
                    "array_binds": 1,
                    "planning_ms": plan["Planning Time"],
                    "execution_ms": plan["Execution Time"],
                    "plan_node": plan["Plan"]["Node Type"],
                    "returned_users": plan["Plan"]["Actual Rows"],
                }
            )
        )
