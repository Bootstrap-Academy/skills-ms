"""Regressions for advisory coverage, incomplete scans and OSV alias handling."""

import json
from pathlib import Path
from typing import Any

from scripts import audit_dependencies as audit

import pytest
from _pytest.monkeypatch import MonkeyPatch

LOCK = b"""
[[package]]
name = "Mako"
version = "1.3.0"
[[package]]
name = "CLICK"
version = "8.1.7"
[[package]]
name = "Pygments"
version = "2.17.2"
"""
MANIFEST = b"""
[tool.poetry.dependencies]
python = "^3.11"
mako = "*"
click = "*"
[tool.poetry.group.dev.dependencies]
pygments = "*"
"""


def advisory(advisory_id: str, package: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": advisory_id,
        "modified": "2026-10-01T00:00:00Z",
        "affected": [{"package": {"ecosystem": "PyPI", "name": package}}],
        **fields,
    }


def test_normalized_names_full_records_and_withdrawn(monkeypatch: MonkeyPatch) -> None:
    details = {
        "GHSA-mako": advisory("GHSA-mako", "Mako", aliases=["CVE-2026-1"], details="Full record"),
        "PYSEC-click": advisory("PYSEC-click", "Click", aliases=["CVE-2026-2"]),
        "GHSA-pygments": advisory("GHSA-pygments", "Pygments", withdrawn="2026-10-02T00:00:00Z"),
    }

    def request(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if payload is None:
            return details[url.rsplit("/", 1)[1]]
        queries = payload["queries"]
        assert [item["package"] for item in queries] == [
            {"name": name, "ecosystem": "PyPI"} for name in ("click", "mako", "pygments")
        ]
        return {"results": [{"vulns": [{"id": name}]} for name in ("PYSEC-click", "GHSA-mako", "GHSA-pygments")]}

    result = audit.scan_lock(LOCK, MANIFEST, request)
    assert result["summary"]["affected_package_versions"] == 2
    assert result["withdrawn_ignored"] == ["GHSA-pygments"]
    assert result["advisories"]["GHSA-mako"]["details"] == "Full record"
    assert result["packages"][0]["scope"] == "runtime-conservative"
    assert result["packages"][2]["scope"] == "non-runtime"
    monkeypatch.setattr(audit, "http_json", request)
    monkeypatch.setattr(Path, "read_bytes", lambda _: LOCK if _.name == "poetry.lock" else MANIFEST)
    assert audit.main([]) == 1


def test_query_pagination_and_alias_transitivity() -> None:
    details = {
        "ID-a": advisory("ID-a", "Mako", aliases=["ALIAS-a"]),
        "ID-b": advisory("ID-b", "Mako", aliases=["ALIAS-b"]),
        "ID-c": advisory("ID-c", "Mako", aliases=["ALIAS-a", "ALIAS-b", "RUSTSEC-2026-0001"]),
    }
    calls = []

    def request(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if payload is None:
            return details[url.rsplit("/", 1)[1]]
        calls.append(payload)
        query = payload["queries"][0]
        if "page_token" not in query:
            return {"results": [{"vulns": [{"id": "ID-a"}], "next_page_token": "next"}]}
        assert query["page_token"] == "next"
        return {"results": [{"vulns": [{"id": "ID-b"}, {"id": "ID-c"}]}]}

    hits, records, queries = audit.query_packages([("Mako", "1.3.0")], request)
    assert hits == {("mako", "1.3.0"): {"ID-a", "ID-b", "ID-c"}}
    assert len(calls) == len(queries) == 2
    assert audit.alias_groups(hits[("mako", "1.3.0")], records) == [
        ["ALIAS-a", "ALIAS-b", "ID-a", "ID-b", "ID-c", "RUSTSEC-2026-0001"]
    ]


@pytest.mark.parametrize(
    "response",
    [{}, {"results": []}, {"results": [None]}, {"results": [{"error": "bad"}]}, {"results": [{"vulns": {}}]}],
)
def test_malformed_batch_cannot_report_clean(response: dict[str, Any]) -> None:
    with pytest.raises(audit.AuditError):
        audit.query_packages([("Mako", "1.3.0")], lambda *_: response)


@pytest.mark.parametrize(
    "malformed",
    [
        {"aliases": None},
        {"withdrawn": ""},
        {"withdrawn": "2026-10-02T00:00:00"},
        {"modified": "not-a-date"},
        {"modified": "2026-13-01T00:00:00Z"},
        {"affected": []},
        {"id": "wrong"},
        {"error": "unavailable"},
    ],
)
def test_malformed_details_cannot_report_clean(malformed: dict[str, Any]) -> None:
    def request(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if payload is not None:
            return {"results": [{"vulns": [{"id": "ID-a"}]}]}
        return advisory("ID-a", "Mako", **malformed)

    with pytest.raises(audit.AuditError):
        audit.query_packages([("Mako", "1.3.0")], request)


def test_network_failure_cli_is_incomplete_and_writes_no_clean_report(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    lock = tmp_path / "poetry.lock"
    manifest = tmp_path / "pyproject.toml"
    output = tmp_path / "audit.json"
    lock.write_bytes(LOCK)
    manifest.write_bytes(MANIFEST)

    def unavailable(*_: Any) -> dict[str, Any]:
        raise OSError("OSV unavailable")

    monkeypatch.setattr(audit, "http_json", unavailable)
    assert audit.main(["--lock", str(lock), "--manifest", str(manifest), "--output", str(output)]) == 2
    assert not output.exists()


def test_repeated_pagination_is_incomplete() -> None:
    with pytest.raises(audit.AuditError, match="repeated"):
        audit.query_packages([("Mako", "1.3.0")], lambda *_: {"results": [{"vulns": [], "next_page_token": "same"}]})


def test_clean_successful_scan_has_zero_exit(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    lock = tmp_path / "poetry.lock"
    manifest = tmp_path / "pyproject.toml"
    output = tmp_path / "audit.json"
    lock.write_bytes(LOCK)
    manifest.write_bytes(MANIFEST)
    monkeypatch.setattr(audit, "http_json", lambda *_: {"results": [{}, {}, {}]})
    assert audit.main(["--lock", str(lock), "--manifest", str(manifest), "--output", str(output)]) == 0
    assert json.loads(output.read_text())["summary"]["raw_advisory_ids"] == 0


def test_known_bad_and_minimum_patched_versions() -> None:
    """Observed OSV IDs and aliases, including the Click PYSEC-only query hit.

    Primary records: https://api.osv.dev/v1/vulns/{id}. Fresh API controls belong
    to release qualification; this deterministic fixture prevents name/scope
    filters or a GitHub-only record policy from hiding these known findings.
    """
    observed = {
        ("click", "8.1.7"): ["PYSEC-2026-2132"],
        ("mako", "1.3.0"): ["GHSA-2h4p-vjrc-8xpq", "PYSEC-2026-2617", "GHSA-v92g-xgxw-vvmm", "PYSEC-2026-88"],
        ("pygments", "2.17.2"): ["GHSA-5239-wwwm-4pmq", "PYSEC-2026-2987"],
        ("click", "8.3.3"): [],
        ("mako", "1.3.12"): [],
        ("pygments", "2.20.0"): [],
    }
    details = {}
    for package, ids, cve in [
        ("click", ["PYSEC-2026-2132", "GHSA-47fr-3ffg-hgmw"], "CVE-2026-7246"),
        ("mako", ["GHSA-2h4p-vjrc-8xpq", "PYSEC-2026-2617"], "CVE-2026-44307"),
        ("mako", ["GHSA-v92g-xgxw-vvmm", "PYSEC-2026-88"], "CVE-2026-41205"),
        ("pygments", ["GHSA-5239-wwwm-4pmq", "PYSEC-2026-2987"], "CVE-2026-4539"),
    ]:
        for advisory_id in ids:
            details[advisory_id] = advisory(advisory_id, package, aliases=[cve, *(set(ids) - {advisory_id})])

    def request(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if payload is None:
            return details[url.rsplit("/", 1)[1]]
        return {
            "results": [
                {
                    "vulns": [
                        {"id": advisory_id} for advisory_id in observed[(query["package"]["name"], query["version"])]
                    ]
                }
                for query in payload["queries"]
            ]
        }

    bad = audit.scan_lock(LOCK, MANIFEST, request)
    assert bad["summary"]["affected_package_versions"] == 3
    assert bad["summary"]["raw_advisory_ids"] == 7
    assert bad["summary"]["deduplicated_advisories"] == 4
    assert bad["packages"][0]["vulnerabilities"] == ["PYSEC-2026-2132"]
    assert bad["packages"][0]["scope"] == bad["packages"][1]["scope"] == "runtime-conservative"
    assert bad["packages"][2]["scope"] == "non-runtime"
    patched = LOCK.replace(b"8.1.7", b"8.3.3").replace(b"1.3.0", b"1.3.12").replace(b"2.17.2", b"2.20.0")
    good = audit.scan_lock(patched, MANIFEST, request)
    assert good["summary"]["package_versions"] == 3
    assert good["summary"]["affected_package_versions"] == good["summary"]["raw_advisory_ids"] == 0


def test_pep503_names_and_related_records_are_not_aliases() -> None:
    assert audit.normalize("Some._PROJECT--Name") == "some-project-name"
    records = {"ID-a": advisory("ID-a", "Mako", related=["ID-b"], upstream=["ID-b"]), "ID-b": advisory("ID-b", "Mako")}
    assert audit.alias_groups(set(records), records) == [["ID-a"], ["ID-b"]]


def test_main_extras_all_platforms_and_all_locked_versions_are_audited() -> None:
    manifest = b"""
[tool.poetry.dependencies]
python = "^3.11"
Root_Package = {version = "*", extras = ["Active_Extra"], platform = "win32"}
InactiveRoot = {version = "*", optional = true}
[tool.poetry.group.dev.dependencies]
Root_Package = {version = "*", extras = ["dev"]}
"""
    lock = b"""
[[package]]
name = "root-package"
version = "1.0"
[package.dependencies]
Optional_Dep = {version = "*", optional = true}
DevDep = {version = "*", optional = true}
Windows_Dep = {version = "*", markers = "sys_platform == 'win32'"}
[package.extras]
active-extra = ["Optional_Dep (>=1)"]
dev = ["DevDep (>=1)"]
[[package]]
name = "optional-dep"
version = "1.0"
[[package]]
name = "optional-dep"
version = "2.0"
[[package]]
name = "Windows_Dep"
version = "1.0"
[[package]]
name = "devdep"
version = "1.0"
[[package]]
name = "InactiveRoot"
version = "1.0"
"""
    queried: list[tuple[str, str]] = []

    def request(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        assert payload is not None
        queried.extend((query["package"]["name"], query["version"]) for query in payload["queries"])
        return {"results": [{} for _ in payload["queries"]]}

    result = audit.scan_lock(lock, manifest, request)
    assert len(queried) == result["summary"]["package_versions"] == 6
    assert result["summary"]["runtime_package_versions_conservative"] == 4
    assert {record["name"] for record in result["packages"] if record["scope"] == "runtime-conservative"} == {
        "root-package",
        "optional-dep",
        "windows-dep",
    }
