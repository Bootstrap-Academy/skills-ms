import re
from pathlib import Path

import pydantic
from pydantic import BaseModel, ConstrainedStr
from yaml import safe_load

from api.logger import get_logger
from api.services.courses import COURSES


class ID(ConstrainedStr):
    regex = re.compile("^[a-z0-9_]+$")


logger = get_logger(__name__)


class SubSkillDescription(BaseModel):
    name: str
    dependencies: list[ID] = []
    courses: list[ID] = []


class RootSkillDescription(BaseModel):
    name: str
    dependencies: list[ID] = []
    skills: dict[ID, SubSkillDescription] = {}


def _load_skills() -> dict[str, RootSkillDescription]:
    skills = {}
    for file in sorted(Path("config/skills").glob("*.yml")):
        name = file.name.removesuffix(".yml")
        logger.debug(f"loading root skill {name} from {file}")
        with file.open() as f:
            skills[name] = pydantic.parse_obj_as(RootSkillDescription, safe_load(f))
    return skills


def _check_skills_definitions() -> None:
    root_skills: set[str] = set()

    for root_id, root_skill in SKILLS.items():
        if root_id in root_skills:
            raise ValueError(f"Root skill {root_id} is defined multiple times!")
        root_skills.add(root_id)

        sub_skills: set[str] = set()
        for sub_id in root_skill.skills:
            if sub_id in sub_skills:
                raise ValueError(f"Sub skill {sub_id} ({root_id}) is defined multiple times!")
            sub_skills.add(sub_id)

    logger.debug("skills definitions are valid")


def _check_skill_dependencies() -> None:
    for root_id, root_skill in SKILLS.items():
        for dependency in root_skill.dependencies:
            if dependency not in SKILLS:
                raise ValueError(f"Root skill {root_id} depends on {dependency}, but {dependency} is not defined!")
        for sub_id, sub_skill in root_skill.skills.items():
            for dependency in sub_skill.dependencies:
                if dependency not in root_skill.skills:
                    raise ValueError(
                        f"Sub skill {sub_id} ({root_id}) depends on {dependency}, but {dependency} is not defined!"
                    )

    logger.debug("skills dependencies are valid")


def _check_skill_courses() -> None:
    for root_id, root_skill in SKILLS.items():
        for sub_id, sub_skill in root_skill.skills.items():
            for course in sub_skill.courses:
                if course not in COURSES:
                    raise ValueError(
                        f"Sub skill {sub_id} ({root_id}) contains course {course}, but {course} is not defined!"
                    )

    logger.debug("skills courses are valid")


SKILLS: dict[str, RootSkillDescription] = _load_skills()
_check_skills_definitions()
_check_skill_dependencies()
_check_skill_courses()
