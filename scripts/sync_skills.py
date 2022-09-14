import argparse
import re
from graphlib import TopologicalSorter
from pathlib import Path

import pydantic
import yaml
from httpx import Client
from pydantic import BaseModel, ConstrainedStr
from rich import print

from api.logger import get_logger


logger = get_logger(__name__)


class ID(ConstrainedStr):
    regex = re.compile("^[a-z0-9_]+$")


class SubSkillDescription(BaseModel):
    name: str
    dependencies: list[ID] = []
    courses: list[ID] = []


class RootSkillDescription(BaseModel):
    name: str
    dependencies: list[ID] = []
    skills: dict[ID, SubSkillDescription] = {}


def _load_skills(path: Path) -> dict[str, RootSkillDescription]:
    skills = {}
    for file in path.glob("*.yml"):
        name = file.name.removesuffix(".yml")
        logger.debug(f"loading root skill {name} from {file}")
        with file.open() as f:
            skills[name] = pydantic.parse_obj_as(RootSkillDescription, yaml.safe_load(f))
    return skills


def _check_skills_definitions(skills: dict[str, RootSkillDescription]) -> None:
    root_skills: set[str] = set()
    sub_skills: set[str] = set()

    for root_id, root_skill in skills.items():
        if root_id in root_skills:
            raise ValueError(f"Root skill {root_id} is defined multiple times!")
        root_skills.add(root_id)

        for sub_id in root_skill.skills:
            if sub_id in sub_skills:
                raise ValueError(f"Sub skill {sub_id} ({root_id}) is defined multiple times!")
            sub_skills.add(sub_id)

    logger.debug("skills definitions are valid")


def _check_skill_dependencies(skills: dict[str, RootSkillDescription]) -> None:
    for root_id, root_skill in skills.items():
        for dependency in root_skill.dependencies:
            if dependency not in skills:
                raise ValueError(f"Root skill {root_id} depends on {dependency}, but {dependency} is not defined!")
        for sub_id, sub_skill in root_skill.skills.items():
            for dependency in sub_skill.dependencies:
                if dependency not in root_skill.skills:
                    raise ValueError(
                        f"Sub skill {sub_id} ({root_id}) depends on {dependency}, but {dependency} is not defined!"
                    )

    logger.debug("skills dependencies are valid")


def _check_skill_courses(skills: dict[str, RootSkillDescription], courses: set[str]) -> None:
    for root_id, root_skill in skills.items():
        for sub_id, sub_skill in root_skill.skills.items():
            for course in sub_skill.courses:
                if course not in courses:
                    raise ValueError(
                        f"Sub skill {sub_id} ({root_id}) contains course {course}, but {course} is not defined!"
                    )

    logger.debug("skills courses are valid")


def main(_list: bool, dry: bool, host: str, token: str, path: Path):
    skills = _load_skills(path)
    _check_skills_definitions(skills)
    _check_skill_dependencies(skills)
    # _check_skill_courses(skills, courses)
    print(skills)

    if _list or not host or not token:
        return

    with Client(base_url=host, headers={"Authorization": token}) as client:
        response = client.get("/skilltree")
        response.raise_for_status()
        remote_skills = {skill["id"]: skill for skill in response.json()}

        add = {*skills} - {*remote_skills}
        update = {*skills} & {*remote_skills}
        delete = {*remote_skills} - {*skills}

        ts = TopologicalSorter({skill_id: {*skills[skill_id].dependencies} & add for skill_id in add})
        for skill in ts.static_order():
            logger.info(f"adding skill {skill}")
            if not dry:
                response = client.post(
                    "/skilltree",
                    json={"id": skill, "name": skills[skill].name, "dependencies": skills[skill].dependencies},
                )
                response.raise_for_status()

        for skill in update:
            diff = {}
            if skills[skill].name != remote_skills[skill]["name"]:
                diff["name"] = skills[skill].name
            if set(skills[skill].dependencies) != set(remote_skills[skill]["dependencies"]):
                diff["dependencies"] = skills[skill].dependencies
            if diff:
                logger.info(f"updating skill {skill}")
                if not dry:
                    response = client.patch(f"/skilltree/{skill}", json=diff)
                    response.raise_for_status()

        for skill in add | update:
            if skill in add:
                remote_sub_skills = {}
            else:
                resp = client.get(f"/skilltree/{skill}")
                resp.raise_for_status()
                remote_sub_skills = {sub_skill["id"]: sub_skill for sub_skill in resp.json()}

            sub_skills = skills[skill].skills

            _add = {*sub_skills} - {*remote_sub_skills}
            _update = {*sub_skills} & {*remote_sub_skills}
            _delete = {*remote_sub_skills} - {*sub_skills}

            ts = TopologicalSorter(
                {sub_skill_id: {*sub_skills[sub_skill_id].dependencies} & _add for sub_skill_id in _add}
            )
            for sub_skill in ts.static_order():
                logger.info(f"adding sub skill {sub_skill} ({skill})")
                if not dry:
                    response = client.post(
                        f"/skilltree/{skill}",
                        json={
                            "id": sub_skill,
                            "name": sub_skills[sub_skill].name,
                            "dependencies": sub_skills[sub_skill].dependencies,
                            "courses": sub_skills[sub_skill].courses,
                        },
                    )
                    response.raise_for_status()

            for sub_skill in _update:
                diff = {}
                if sub_skills[sub_skill].name != remote_sub_skills[sub_skill]["name"]:
                    diff["name"] = sub_skills[sub_skill].name
                if set(sub_skills[sub_skill].dependencies) != set(remote_sub_skills[sub_skill]["dependencies"]):
                    diff["dependencies"] = sub_skills[sub_skill].dependencies
                if set(sub_skills[sub_skill].courses) != set(remote_sub_skills[sub_skill]["courses"]):
                    diff["courses"] = sub_skills[sub_skill].courses
                if diff:
                    logger.info(f"updating sub skill {sub_skill} ({skill})")
                    if not dry:
                        response = client.patch(f"/skilltree/{skill}/{sub_skill}", json=diff)
                        response.raise_for_status()

            for sub_skill in _delete:
                logger.info(f"deleting sub skill {sub_skill} ({skill})")
                if not dry:
                    response = client.delete(f"/skilltree/{skill}/{sub_skill}")
                    response.raise_for_status()

        for skill in delete:
            logger.info(f"deleting skill {skill}")
            if not dry:
                response = client.delete(f"/skilltree/{skill}")
                response.raise_for_status()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync skills from yaml files to the backend.")
    parser.add_argument("--list", action="store_true", help="list all skills without syncing")
    parser.add_argument("--dry", action="store_true", help="dry run")
    parser.add_argument("--host", metavar="host", type=str, help="Host of the backend")
    parser.add_argument("--token", metavar="token", type=str, help="Token for the backend")
    parser.add_argument("path", metavar="path", type=Path, help="Path to the yaml files")
    args = parser.parse_args()
    main(_list=args.list, dry=args.dry, host=args.host, token=args.token, path=args.path)
