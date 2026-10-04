"""Audit every Poetry lock package against OSV, including non-GitHub records.

Run with Python 3.11+: python scripts/audit_dependencies.py --output audit.json
Exit codes: 0 = no known active advisories; 1 = findings; 2 = incomplete audit.
Runtime scope starts at the main Poetry group, activates its extras and includes
all platforms. It does not prove installation or exploitability in production.
"""

import argparse
import hashlib
import json
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

OSV_API = "https://api.osv.dev/v1"
JsonRequest = Callable[[str, dict[str, Any] | None], dict[str, Any]]


class AuditError(ValueError):
    """The service response cannot establish a complete audit."""


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def http_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "skills-ms-audit"})
    with urlopen(request, timeout=30) as response:  # noqa: S310
        result = json.load(response)
    if not isinstance(result, dict) or "error" in result:
        raise AuditError("OSV returned an invalid or error response")
    return result


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise AuditError("OSV returned an invalid advisory identifier")
    return value


def timestamp(value: Any, field: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise AuditError(f"OSV returned invalid {field} metadata")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AuditError(f"OSV returned invalid {field} metadata") from error


def query_packages(
    packages: list[tuple[str, str]], request: JsonRequest = http_json
) -> tuple[dict[tuple[str, str], set[str]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Follow each query's pagination, then fetch every complete advisory."""
    packages = sorted({(normalize(name), version) for name, version in packages})
    hits: dict[tuple[str, str], set[str]] = {package: set() for package in packages}
    pending = [(package, "") for package in packages]
    seen_pages: set[tuple[tuple[str, str], str]] = set()
    queries = []
    while pending:
        following = []
        for start in range(0, len(pending), 1000):
            end = start + 1000
            group = pending[start:end]
            payload = []
            for (name, version), token in group:
                item = {"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
                if token:
                    item["page_token"] = token
                payload.append(item)
            result = request(f"{OSV_API}/querybatch", {"queries": payload})
            if not isinstance(result, dict):
                raise AuditError("OSV querybatch response must be an object")
            results = result.get("results")
            if "error" in result or not isinstance(results, list) or len(results) != len(group):
                raise AuditError("OSV querybatch result count or schema is invalid")
            queries.append({"request": payload, "response": result})
            for (package, _), item in zip(group, results):
                if not isinstance(item, dict) or set(item) - {"vulns", "next_page_token"}:
                    raise AuditError("OSV querybatch package result is invalid")
                vulns = item.get("vulns", [])
                if not isinstance(vulns, list):
                    raise AuditError("OSV querybatch vulnerabilities must be a list")
                for vuln in vulns:
                    if not isinstance(vuln, dict):
                        raise AuditError("OSV querybatch vulnerability is invalid")
                    hits[package].add(identifier(vuln.get("id")))
                token = item.get("next_page_token", "")
                if not isinstance(token, str):
                    raise AuditError("OSV pagination token is invalid")
                if token:
                    page = (package, token)
                    if page in seen_pages:
                        raise AuditError("OSV repeated a pagination token")
                    seen_pages.add(page)
                    following.append(page)
        pending = following

    advisories = {}
    advisory_ids = {advisory_id for ids in hits.values() for advisory_id in ids}
    for advisory_id in sorted(advisory_ids):
        advisory = request(f"{OSV_API}/vulns/{quote(advisory_id, safe='')}", None)
        if not isinstance(advisory, dict) or "error" in advisory:
            raise AuditError(f"OSV advisory {advisory_id} response is invalid")
        if advisory.get("id") != advisory_id or not isinstance(advisory.get("affected"), list):
            raise AuditError(f"OSV advisory {advisory_id} has an invalid ID or affected field")
        timestamp(advisory.get("modified"), "modified")
        aliases = advisory.get("aliases", [])
        if not isinstance(aliases, list):
            raise AuditError(f"OSV advisory {advisory_id} has invalid aliases")
        for alias in aliases:
            identifier(alias)
        if "withdrawn" in advisory:
            timestamp(advisory["withdrawn"], "withdrawn")
        for affected in advisory["affected"]:
            if not isinstance(affected, dict) or not isinstance(affected.get("package"), dict):
                raise AuditError(f"OSV advisory {advisory_id} has invalid affected package metadata")
            package = affected["package"]
            if not isinstance(package.get("name"), str) or not isinstance(package.get("ecosystem"), str):
                raise AuditError(f"OSV advisory {advisory_id} has invalid affected package metadata")
        advisories[advisory_id] = advisory
    for (name, _), ids in hits.items():
        for advisory_id in ids:
            if not any(
                affected["package"]["ecosystem"] == "PyPI" and normalize(affected["package"]["name"]) == name
                for affected in advisories[advisory_id]["affected"]
            ):
                raise AuditError(f"OSV advisory {advisory_id} does not describe the queried package {name}")
    return hits, advisories, queries


def alias_groups(ids: set[str], advisories: dict[str, dict[str, Any]]) -> list[list[str]]:
    """Merge alias-connected records transitively without dropping their IDs."""
    groups: list[set[str]] = []
    for advisory_id in sorted(ids):
        group = {advisory_id, *advisories[advisory_id].get("aliases", [])}
        separate = []
        for existing in groups:
            if existing & group:
                group.update(existing)
            else:
                separate.append(existing)
        groups = [*separate, group]
    return sorted(sorted(group) for group in groups)


def dependency_entries(spec: Any) -> list[dict[str, Any]]:
    if isinstance(spec, str):
        return [{"version": spec}]
    if isinstance(spec, dict):
        return [spec]
    if isinstance(spec, list) and all(isinstance(item, dict) for item in spec):
        return spec
    raise AuditError("Poetry dependency specification is invalid")


def runtime_closure(manifest: dict[str, Any], packages: dict[str, list[dict[str, Any]]]) -> set[str]:
    """Activate main-group extras only; platform markers remain conservative."""
    pending = [
        (normalize(name), {normalize(extra) for extra in entry.get("extras", [])})
        for name, spec in manifest["tool"]["poetry"]["dependencies"].items()
        if name != "python"
        for entry in dependency_entries(spec)
        if not entry.get("optional")
    ]
    seen: dict[str, set[str]] = {}
    while pending:
        name, extras = pending.pop()
        if name in seen and extras <= seen[name]:
            continue
        if name not in packages:
            raise AuditError(f"Runtime dependency {name} is missing from the lock")
        seen.setdefault(name, set()).update(extras)
        for package in packages[name]:
            selected_optional = set()
            for extra, requirements in package.get("extras", {}).items():
                if normalize(extra) in seen[name]:
                    for requirement in requirements:
                        match = re.match(r"[A-Za-z0-9_.-]+", requirement)
                        if match is None:
                            raise AuditError("Poetry extra requirement is invalid")
                        selected_optional.add(normalize(match[0]))
            for dependency, spec in package.get("dependencies", {}).items():
                for entry in dependency_entries(spec):
                    if entry.get("optional") and normalize(dependency) not in selected_optional:
                        continue
                    pending.append((normalize(dependency), {normalize(extra) for extra in entry.get("extras", [])}))
    return set(seen)


def scan_lock(lock_data: bytes, manifest_data: bytes, request: JsonRequest = http_json) -> dict[str, Any]:
    lock = tomllib.loads(lock_data.decode("utf-8"))
    manifest = tomllib.loads(manifest_data.decode("utf-8"))
    packages = lock.get("package")
    if not isinstance(packages, list) or not packages:
        raise AuditError("Poetry lock has no packages")
    by_name: dict[str, list[dict[str, Any]]] = {}
    versions = []
    for package in packages:
        name, version = package.get("name"), package.get("version")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise AuditError("Poetry lock has an invalid package")
        if package.get("source", {}).get("type") not in (None, "legacy"):
            raise AuditError(f"Cannot audit non-registry package {name} against PyPI")
        name = normalize(name)
        by_name.setdefault(name, []).append(package)
        versions.append((name, version))
    runtime = runtime_closure(manifest, by_name)
    hits, advisories, queries = query_packages(versions, request)
    records = []
    for (name, version), ids in sorted(hits.items()):
        active = {advisory_id for advisory_id in ids if "withdrawn" not in advisories[advisory_id]}
        records.append(
            {
                "name": name,
                "version": version,
                "scope": "runtime-conservative" if name in runtime else "non-runtime",
                "queried_ids": sorted(ids),
                "vulnerabilities": sorted(active),
                "alias_groups": alias_groups(active, advisories),
            }
        )
    active_ids = {advisory_id for record in records for advisory_id in record["vulnerabilities"]}
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "lock_sha256": hashlib.sha256(lock_data).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_data).hexdigest(),
        "source": OSV_API,
        "scope": "Known OSV advisories for every PyPI lock package; excludes unpublished and system-library issues.",
        "runtime_scope": (
            "Non-optional main Poetry dependencies with their activated extras; all platform markers included. "
            "Non-runtime includes development and inactive optional dependencies; project extras are not enabled."
        ),
        "pagination_complete": True,
        "queries": queries,
        "packages": records,
        "advisories": advisories,
        "withdrawn_ignored": sorted(advisory_id for advisory_id, item in advisories.items() if "withdrawn" in item),
        "summary": {
            "package_versions": len(records),
            "runtime_package_versions_conservative": sum(
                record["scope"] == "runtime-conservative" for record in records
            ),
            "affected_package_versions": sum(bool(record["vulnerabilities"]) for record in records),
            "raw_advisory_assignments": sum(len(record["vulnerabilities"]) for record in records),
            "deduplicated_advisory_assignments": sum(len(record["alias_groups"]) for record in records),
            "raw_advisory_ids": len(active_ids),
            "deduplicated_advisories": len(alias_groups(active_ids, advisories)),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("poetry.lock"))
    parser.add_argument("--manifest", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = scan_lock(args.lock.read_bytes(), args.manifest.read_bytes(), request=http_json)
    except (OSError, URLError, ValueError, KeyError, TypeError) as error:
        print(f"Dependency audit incomplete: {error}", file=sys.stderr)
        return 2
    output = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(output)
        print(json.dumps(result["summary"]))
    else:
        print(output, end="")
    return int(bool(result["summary"]["affected_package_versions"]))


if __name__ == "__main__":
    sys.exit(main())
