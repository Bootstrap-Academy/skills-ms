import pydantic
from pydantic import BaseModel
from yaml import safe_load

from api.logger import get_logger


logger = get_logger(__name__)


class SkillDescription(BaseModel):
    name: str
    dependencies: list[str]


def _load_skills() -> dict[str, SkillDescription]:
    with open("skills.yml") as f:
        return pydantic.parse_obj_as(dict[str, SkillDescription], safe_load(f))


def _check_skill_dependencies() -> None:
    for skill, description in SKILLS.items():
        for dependency in description.dependencies:
            if dependency not in SKILLS:
                raise ValueError(f"Skill {skill} depends on {dependency}, but {dependency} is not defined!")

    logger.debug("skills dependencies are valid")


SKILLS: dict[str, SkillDescription] = _load_skills()
_check_skill_dependencies()
