"""Fresh, fail-closed publication authority, separate from identity caches."""

from typing import Literal, TypedDict, TypeVar, cast
from uuid import UUID

from fastapi import HTTPException
from httpx import HTTPError
from pydantic import BaseModel, Field, StrictBool, StrictStr, ValidationError, conint, root_validator

from api.services.internal import InternalService
from api.settings import settings


SCOPE_VERSION: Literal["academy-verified-v1"] = "academy-verified-v1"
HEADERS = {"Cache-Control": "private, no-store", "Vary": "Authorization"}


def unavailable() -> HTTPException:
    return HTTPException(503, "Profile publication temporarily unavailable", headers=HEADERS)


class RankingMetadata(TypedDict):
    scope_version: str
    publication_epoch: UUID
    epoch_revision: int


class Epoch(BaseModel):
    scope_version: Literal["academy-verified-v1"]
    publication_epoch: UUID
    epoch_revision: conint(strict=True, ge=0)  # type: ignore[valid-type]
    policy_active: StrictBool
    publishing_enabled: StrictBool

    class Config:
        extra = "forbid"

    @root_validator(skip_on_failure=True)
    @classmethod
    def coherent_policy(cls, values: dict[str, object]) -> dict[str, object]:
        if values["publishing_enabled"] and not values["policy_active"]:
            raise ValueError("Publication policy is not active")
        return values

    @property
    def ranking_metadata(self) -> RankingMetadata:
        return {
            "scope_version": self.scope_version,
            "publication_epoch": self.publication_epoch,
            "epoch_revision": self.epoch_revision,
        }

    def require_enabled(self) -> None:
        if not (self.policy_active and self.publishing_enabled and settings.profile_publications_enabled):
            raise unavailable()


class Participant(BaseModel):
    user_id: UUID
    visibility_revision: conint(strict=True, ge=1)  # type: ignore[valid-type]
    display_name: StrictStr
    avatar_url: None = Field(...)

    class Config:
        extra = "forbid"


class Snapshot(Epoch):
    participants: tuple[Participant, ...]

    @root_validator(skip_on_failure=True)
    @classmethod
    def unique_participants(cls, values: dict[str, object]) -> dict[str, object]:
        participants = cast(tuple[Participant, ...], values["participants"])
        if len({person.user_id for person in participants}) != len(participants):
            raise ValueError("Duplicate publication participant")
        return values

    @property
    def user_ids(self) -> tuple[str, ...]:
        return tuple(str(person.user_id) for person in self.participants)

    @property
    def epoch(self) -> Epoch:
        return Epoch.parse_obj(self.dict(exclude={"participants"}))


E = TypeVar("E", bound=Epoch)


async def _read(path: str, model: type[E]) -> E:
    try:
        async with InternalService.AUTH.client as client:
            # All authority failures use the same closed response; no generic
            # internal hook may turn an auth outage into a cached permission.
            client.event_hooks["response"] = []
            response = await client.get(f"/profile-publications/{path}")
        if response.status_code != 200:
            raise unavailable()
        return model.parse_obj(response.json())
    except (HTTPError, ValidationError, ValueError, TypeError):
        raise unavailable() from None


async def current_epoch() -> Epoch:
    return await _read("epoch", Epoch)


async def current_snapshot() -> Snapshot:
    result = await _read("snapshot", Snapshot)
    result.require_enabled()
    return result


async def use_shared_rankings() -> bool:
    """The persistent backend policy fences rollback of a local feature flag."""
    epoch = await current_epoch()
    if not epoch.policy_active and not settings.profile_publications_enabled:
        return False
    epoch.require_enabled()
    return True
