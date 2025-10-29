import argparse
import hashlib
import re
import uuid
from datetime import timedelta
from graphlib import TopologicalSorter
from pathlib import Path

import pydantic
import yaml
from httpx import Client
from pydantic import BaseModel, ConstrainedStr
from rich import print

from api.logger import get_logger
from api.services.courses import COURSES
from api.settings import settings
from api.utils.jwt import encode_jwt


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
    for file in sorted(path.glob("*.yml")):
        name = file.name.removesuffix(".yml")
        logger.debug(f"loading root skill {name} from {file}")
        with file.open(encoding="utf-8") as f:
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


def _get_position(name: str) -> dict[str, int]:
    row = int(hashlib.sha256(f"row:{name}".encode()).hexdigest(), 16) % 20
    col = int(hashlib.sha256(f"col:{name}".encode()).hexdigest(), 16) % 20
    return {"row": row, "column": col}


def _normalize_host(host: str) -> str:
    return host.rstrip("/")


def _ensure_bearer(token: str) -> str:
    token = token.strip()
    if not token:
        raise ValueError("Token must not be empty when authentication is required.")
    if token.lower().startswith("bearer "):
        return token
    return f"Bearer {token}"


def _read_token_from_file(token_file: Path | None) -> str:
    if token_file is None:
        return ""
    if not token_file.is_file():
        raise FileNotFoundError(f"Token file {token_file} does not exist.")
    return token_file.read_text(encoding="utf-8").strip()


def _generate_admin_token(admin_id: str, ttl_seconds: int) -> str:
    payload = {
        "uid": admin_id,
        "rt": f"sync-skills:{uuid.uuid4().hex}",
        "data": {"admin": True, "email_verified": True},
    }
    return encode_jwt(payload, timedelta(seconds=ttl_seconds))


def _resolve_token(
    token: str,
    token_file: Path | None,
    *,
    admin_id: str,
    ttl_seconds: int,
    no_auth: bool,
) -> str | None:
    if no_auth:
        logger.debug("Skipping authentication as requested via --no-auth.")
        return None

    if ttl_seconds <= 0:
        raise ValueError("--token-ttl must be greater than zero seconds.")

    if token:
        return _ensure_bearer(token)

    file_token = _read_token_from_file(token_file)
    if file_token:
        logger.debug("Using token read from %s.", token_file)
        return _ensure_bearer(file_token)

    generated = _generate_admin_token(admin_id, ttl_seconds)
    logger.debug("Generated short-lived admin token for %s (ttl=%s seconds).", admin_id, ttl_seconds)
    return _ensure_bearer(generated)


def _export_remote_skills(path: Path, client: Client, *, overwrite: bool) -> None:
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)

    response = client.get("/skilltree")
    response.raise_for_status()
    payload = response.json()
    remote_skills = payload.get("skills", [])

    for skill in sorted(remote_skills, key=lambda item: item["id"]):
        detail_response = client.get(f"/skilltree/{skill['id']}")
        detail_response.raise_for_status()
        sub_tree = detail_response.json().get("skills", [])

        root_description = RootSkillDescription(
            name=skill["name"],
            dependencies=list(skill.get("dependencies", [])),
            skills={
                sub["id"]: SubSkillDescription(
                    name=sub["name"],
                    dependencies=list(sub.get("dependencies", [])),
                    courses=list(sub.get("courses", [])),
                )
                for sub in sorted(sub_tree, key=lambda item: item["id"])
            },
        )

        destination_file = path / f"{skill['id']}.yml"
        if destination_file.exists() and not overwrite:
            raise FileExistsError(
                f"{destination_file} already exists. Pass --overwrite to replace existing files."
            )

        with destination_file.open("w", encoding="utf-8") as file_handle:
            yaml.safe_dump(
                root_description.dict(),
                file_handle,
                sort_keys=False,
                allow_unicode=True,
            )
        logger.info("Exported %s to %s", skill["id"], destination_file)


def main(
    path: Path,
    *,
    _list: bool = False,
    dry: bool = False,
    update_positions: bool = False,
    host: str = "",
    token: str = "",
    token_file: Path | None = None,
    admin_id: str = "sync-skills-cli",
    token_ttl: int = 600,
    no_auth: bool = False,
    pull: bool = False,
    overwrite: bool = False,
):
    if pull:
        resolved_host = _normalize_host(host or settings.public_base_url)
        if not resolved_host:
            raise ValueError("A host must be provided when using --pull.")
        if "://" not in resolved_host:
            raise ValueError(f"Host '{resolved_host}' must include a scheme such as http:// or https://.")

        token_header = _resolve_token(token, token_file, admin_id=admin_id, ttl_seconds=token_ttl, no_auth=no_auth)
        headers = {"Authorization": token_header} if token_header else None

        with Client(base_url=resolved_host, headers=headers) as client:
            _export_remote_skills(path, client, overwrite=overwrite)

        return

    if not path.is_dir():
        raise NotADirectoryError(f"Path {path} must be a directory containing skill YAML files.")

    skills = _load_skills(path)
    _check_skills_definitions(skills)
    _check_skill_dependencies(skills)
    _check_skill_courses(skills, set(COURSES))
    print(skills)

    if _list:
        return

    resolved_host = _normalize_host(host or settings.public_base_url)
    if not resolved_host:
        raise ValueError("Unable to determine host. Pass --host explicitly or configure PUBLIC_BASE_URL.")
    if "://" not in resolved_host:
        raise ValueError(f"Host '{resolved_host}' must include a scheme such as http:// or https://.")

    token_header = _resolve_token(token, token_file, admin_id=admin_id, ttl_seconds=token_ttl, no_auth=no_auth)
    headers = {"Authorization": token_header} if token_header else None

    with Client(base_url=resolved_host, headers=headers) as client:
        response = client.get("/skilltree")
        response.raise_for_status()
        remote_skills = {skill["id"]: skill for skill in response.json()["skills"]}

        add = {*skills} - {*remote_skills}
        update = {*skills} & {*remote_skills}
        delete = {*remote_skills} - {*skills}

        ts = TopologicalSorter({skill_id: {*skills[skill_id].dependencies} & add for skill_id in add})
        for skill in ts.static_order():
            logger.info(f"adding skill {skill}")
            if not dry:
                response = client.post(
                    "/skilltree",
                    json={
                        "id": skill,
                        "name": skills[skill].name,
                        "dependencies": skills[skill].dependencies,
                        **_get_position(skill),
                        "sub_tree_rows": 20,
                        "sub_tree_columns": 20,
                    },
                )
                response.raise_for_status()

        for skill in update:
            diff = {}
            if skills[skill].name != remote_skills[skill]["name"]:
                diff["name"] = skills[skill].name
            if set(skills[skill].dependencies) != set(remote_skills[skill]["dependencies"]):
                diff["dependencies"] = skills[skill].dependencies
            if update_positions and _get_position(skill) != {
                "row": remote_skills[skill]["row"],
                "column": remote_skills[skill]["column"],
            }:
                diff |= _get_position(skill)
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
                remote_sub_skills = {sub_skill["id"]: sub_skill for sub_skill in resp.json()["skills"]}

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
                            **_get_position(skill + "/" + sub_skill),
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
                if update_positions and _get_position(skill + "/" + sub_skill) != {
                    "row": remote_sub_skills[sub_skill]["row"],
                    "column": remote_sub_skills[sub_skill]["column"],
                }:
                    diff |= _get_position(skill + "/" + sub_skill)
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
    parser.add_argument("--update-positions", action="store_true", help="update positions")
    parser.add_argument(
        "--host",
        metavar="host",
        type=str,
        default=settings.public_base_url,
        help="Host of the skills backend (defaults to PUBLIC_BASE_URL from settings).",
    )
    parser.add_argument("--token", metavar="token", type=str, help="JWT token to use for authentication.")
    parser.add_argument(
        "--token-file",
        metavar="token-file",
        type=Path,
        help="Read the JWT token from the given file (ignored when --token is provided).",
    )
    parser.add_argument(
        "--admin-id",
        metavar="admin-id",
        type=str,
        default="sync-skills-cli",
        help="User ID used when generating a short-lived admin token.",
    )
    parser.add_argument(
        "--token-ttl",
        metavar="seconds",
        type=int,
        default=600,
        help="Lifetime in seconds for generated admin tokens (default: 600).",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Do not send an Authorization header (useful for unsecured or test instances).",
    )
    parser.add_argument(
        "--pull",
        action="store_true",
        help="Fetch skills from the remote host and write YAML files instead of pushing local changes.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow overwriting existing files when using --pull.",
    )
    parser.add_argument("path", metavar="path", type=Path, help="Path to the yaml files")
    args = parser.parse_args()
    main(
        args.path,
        _list=args.list,
        dry=args.dry,
        update_positions=args.update_positions,
        host=args.host,
        token=args.token,
        token_file=args.token_file,
        admin_id=args.admin_id,
        token_ttl=args.token_ttl,
        no_auth=args.no_auth,
        pull=args.pull,
        overwrite=args.overwrite,
    )
