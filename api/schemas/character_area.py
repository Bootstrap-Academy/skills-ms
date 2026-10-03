"""Character areas group existing skill roots without changing their identities."""

from typing import Self

from pydantic import Field, model_validator

from api.schemas.rooms import LocalizedText, RoomModel


class CharacterAreaDefinition(RoomModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    title: LocalizedText
    root_skill_ids: list[str] = Field(default_factory=list)
    include_unassigned_roots: bool = False
    rows: int | None = Field(default=None, ge=1)
    columns: int | None = Field(default=None, ge=1)


class CharacterAreaCatalogue(RoomModel):
    areas: list[CharacterAreaDefinition]

    @model_validator(mode="after")
    def distinct_membership(self) -> Self:
        areas = self.areas
        ids = [area.id for area in areas]
        roots = [root for area in areas for root in area.root_skill_ids]
        if len(ids) != len(set(ids)) or len(roots) != len(set(roots)):
            raise ValueError("Duplicate area or root membership")
        if sum(area.include_unassigned_roots for area in areas) > 1:
            raise ValueError("Only one area may receive unassigned existing roots")
        return self


class CharacterArea(RoomModel):
    id: str
    title: LocalizedText
    root_skill_ids: list[str]


class CharacterAreas(RoomModel):
    areas: list[CharacterArea]
