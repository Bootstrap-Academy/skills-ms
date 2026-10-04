"""Fresh, fail-closed publication authority, separate from identity caches."""

from typing import Literal, Self, TypedDict, TypeVar
from uuid import UUID

from fastapi import HTTPException
from httpx import HTTPError
from pydantic import Field, StrictBool, StrictStr, ValidationError, model_validator

from api.schemas import BaseModel
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
    epoch_revision: int = Field(strict=True, ge=0)
    policy_active: StrictBool
    publishing_enabled: StrictBool

    @model_validator(mode="after")
    def coherent_policy(self) -> Self:
        if self.publishing_enabled and not self.policy_active:
            raise ValueError("Publication policy is not active")
        return self

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
    visibility_revision: int = Field(strict=True, ge=1)
    display_name: StrictStr
    avatar_url: None = Field(...)


class Snapshot(Epoch):
    participants: tuple[Participant, ...]

    @model_validator(mode="after")
    def unique_participants(self) -> Self:
        if len({person.user_id for person in self.participants}) != len(self.participants):
            raise ValueError("Duplicate publication participant")
        return self

    @property
    def user_ids(self) -> tuple[str, ...]:
        return tuple(str(person.user_id) for person in self.participants)

    @property
    def epoch(self) -> Epoch:
        return Epoch.model_validate(self.model_dump(exclude={"participants"}))


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
        return model.model_validate(response.json())
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
