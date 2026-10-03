from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, HTTPException, Query, Response
from pydantic import BaseModel

from api import models
from api.database import db, filter_by, select
from api.exceptions.skill import SkillNotFoundException
from api.schemas.skill import SubSkill
from api.services import publications
from api.services.benefits import XPAward, apply_xp
from api.utils.cache import clear_cache, redis_cached
from api.utils.docs import responses


router = APIRouter()


@router.post("/xp-operations/{operation}/{user_id}/{skill_id}")
async def apply_skill_benefit(operation: UUID, user_id: UUID, skill_id: str, award: XPAward) -> Any:
    result = await apply_xp(str(operation), str(user_id), skill_id, award)
    # A successful response is an existing committed receipt, not merely a
    # mutation waiting for the outer request middleware to commit.
    await db.commit()
    await clear_cache("xp")
    return result


@router.get("/skills", responses=responses(list[SubSkill]))
@redis_cached("skills")
async def get_skills() -> Any:
    """Return a list of all skills."""

    return [skill.serialize async for skill in await db.stream(select(models.SubSkill))]


@router.get("/skills/{skill_id}/dependencies", responses=responses(set[str], SkillNotFoundException))
@redis_cached("skills", "skill_id")
async def get_skill_dependencies(skill_id: str) -> Any:
    """Return a list of all skills that are required to learn this skill."""

    skill = await db.get(models.SubSkill, id=skill_id)
    if not skill:
        raise SkillNotFoundException

    out = {s.id for s in skill.dependencies}
    for root in skill.parent.dependencies:
        out |= {s.id for s in root.sub_skills}

    return out


@router.get("/skills/{user_id}", responses=responses(dict[str, int]))
@redis_cached("xp", "user_id")
async def get_skill_levels(user_id: str) -> Any:
    return await models.XP.get_user_skill_levels(user_id)


@router.get("/graduates/{skill_id}", responses=responses(list[str]))
@redis_cached("xp", "skills")
async def get_graduates(skill_id: str, level: int = Query(ge=0)) -> Any:
    return await models.XP.get_skill_graduates(skill_id, level)


@router.post("/skills/{user_id}/{skill_id}", responses=responses(bool, SkillNotFoundException))
async def add_skill_progress(user_id: str, skill_id: str, xp: int = Body(embed=True)) -> Any:
    """Add progress to a skill for a user."""

    if not await db.exists(filter_by(models.SubSkill, id=skill_id)):
        raise SkillNotFoundException

    await models.XP.add_xp(user_id, skill_id, xp)

    await clear_cache("xp")

    return True


class Rank(BaseModel):
    xp: int
    rank: int


class LeaderboardUser(Rank):
    user: str


class Leaderboard(BaseModel):
    leaderboard: list[LeaderboardUser]
    total: int


class PublishedLeaderboard(Leaderboard):
    scope_version: str
    publication_epoch: UUID
    epoch_revision: int


class PublishedRank(BaseModel):
    xp: int
    rank: int | None
    public_rank: int | None
    scope_version: str
    publication_epoch: UUID
    epoch_revision: int


async def published_leaderboard(
    limit: int, offset: int, publication_epoch: UUID | None = None, scope_version: str | None = None
) -> PublishedLeaderboard:
    for _ in range(2):
        snapshot = await publications.current_snapshot()
        if (publication_epoch is not None and publication_epoch != snapshot.publication_epoch) or (
            scope_version is not None and scope_version != snapshot.scope_version
        ):
            raise HTTPException(409, "Publication changed; reload the leaderboard", headers=publications.HEADERS)
        result = PublishedLeaderboard(
            leaderboard=[
                LeaderboardUser(user=user, xp=xp, rank=rank)
                for user, xp, rank in await models.XP.get_leaderboard(limit, offset, snapshot.user_ids)
            ],
            total=await models.XP.count_users(snapshot.user_ids),
            **snapshot.epoch.ranking_metadata,
        )
        if await publications.current_epoch() == snapshot.epoch:
            return result
    raise publications.unavailable()


async def published_rank(
    user_id: str, publication_epoch: UUID | None = None, scope_version: str | None = None
) -> PublishedRank:
    for _ in range(2):
        snapshot = await publications.current_snapshot()
        if (publication_epoch is not None and publication_epoch != snapshot.publication_epoch) or (
            scope_version is not None and scope_version != snapshot.scope_version
        ):
            raise HTTPException(409, "Publication changed; reload the leaderboard", headers=publications.HEADERS)
        xp = await models.XP.get_user_xp(user_id)
        qualified = user_id in snapshot.user_ids and await db.exists(filter_by(models.XP, user_id=user_id))
        rank = await models.XP.rank_of(xp, snapshot.user_ids) if qualified else None
        result = PublishedRank(xp=xp, rank=rank, public_rank=rank, **snapshot.epoch.ranking_metadata)
        if await publications.current_epoch() == snapshot.epoch:
            return result
    raise publications.unavailable()


@router.get("/published-leaderboard", responses=responses(PublishedLeaderboard))
async def get_published_leaderboard(
    response: Response,
    limit: int = Query(ge=0, le=100),
    offset: int = Query(ge=0),
    publication_epoch: UUID = Query(),
    scope_version: str = Query(),
) -> PublishedLeaderboard:
    response.headers.update(publications.HEADERS)
    return await published_leaderboard(limit, offset, publication_epoch, scope_version)


@router.get("/published-leaderboard/{user_id}", responses=responses(PublishedRank))
async def get_published_rank(
    user_id: UUID, response: Response, publication_epoch: UUID = Query(), scope_version: str = Query()
) -> PublishedRank:
    # This internal score is available for the owner's private card. The
    # caller must never expose it to another user when public_rank is null.
    response.headers.update(publications.HEADERS)
    return await published_rank(str(user_id), publication_epoch, scope_version)


@router.get("/leaderboard", responses=responses(Leaderboard))
async def get_leaderboard(limit: int, offset: int, response: Response) -> PublishedLeaderboard | Leaderboard:
    if await publications.use_shared_rankings():
        response.headers.update(publications.HEADERS)
        return await published_leaderboard(limit, offset)
    return Leaderboard(
        leaderboard=[
            LeaderboardUser(user=user, xp=xp, rank=rank)
            for user, xp, rank in await models.XP.get_leaderboard(limit, offset)
        ],
        total=await models.XP.count_users(),
    )


@router.get("/leaderboard/{user_id}", responses=responses(Rank))
async def get_leaderboard_user(user_id: str, response: Response) -> PublishedRank | Rank:
    if await publications.use_shared_rankings():
        response.headers.update(publications.HEADERS)
        return await published_rank(user_id)
    xp = await models.XP.get_user_xp(user_id)
    return Rank(xp=xp, rank=await models.XP.rank_of(xp))
