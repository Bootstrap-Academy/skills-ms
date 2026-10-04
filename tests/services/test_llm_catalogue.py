"""The server-side `llm_profiles` field and the `llm-verdict` completion in the private catalogue."""

from typing import Any

import pytest
from pydantic import ValidationError

from api.schemas.rooms import CatalogueUnit, IntroductionCompletion, LlmVerdictCompletion
from api.services.rooms import load_catalogue

GRADING = {"kind": "llm-verdict", "profile": "llmb-grade-prompt", "profile_sha256": "ab" * 32}


def unit(**values: Any) -> dict[str, Any]:
    return {
        "id": "graded",
        "path_id": "prompting",
        "title": {"de": "Bewertet", "en": "Graded"},
        "room": "guided-lesson",
        "content": {"de": {"text": "Synthetic"}},
        "teaches": [],
        "practices": [],
        "requires": [],
        "retired": False,
        "completion": {"kind": "introduced", "answer": {"answer": 6}},
        **values,
    }


def test_existing_units_load_unchanged_without_llm_profiles() -> None:
    parsed = CatalogueUnit.model_validate(unit())
    assert parsed.llm_profiles == []
    assert isinstance(parsed.completion, IntroductionCompletion) and parsed.completion.allow_skip is False
    for shipped in load_catalogue().units:
        assert shipped.llm_profiles == []
        assert shipped.completion is None or isinstance(shipped.completion, IntroductionCompletion)


def test_graded_unit_keeps_profiles_and_grading_server_side() -> None:
    parsed = CatalogueUnit.model_validate(
        unit(llm_profiles=["llmb-grade-prompt", "llmb-temperature-fan"], completion=GRADING)
    )
    assert isinstance(parsed.completion, LlmVerdictCompletion)
    assert parsed.completion.allow_skip is True  # the grader can be wrong, skipping stays open
    assert parsed.completion.allow_fallback is True  # and without the model it completes as `introduced`
    assert parsed.llm_profiles == ["llmb-grade-prompt", "llmb-temperature-fan"]
    public = parsed.public().model_dump()
    assert "llm_profiles" not in public and "completion" not in public
    # Only the kind of the check is public: no profile, rubric hash or answer.
    assert public["completion_kind"] == "llm-verdict"
    assert "llmb-grade-prompt" not in str(public) and GRADING["profile_sha256"] not in str(public)
    assert CatalogueUnit.model_validate(unit()).public().completion_kind == "introduced"
    assert "answer" not in str(CatalogueUnit.model_validate(unit()).public().model_dump())
    # A plain LLM unit (no grading) keeps its normal completion.
    assert CatalogueUnit.model_validate(unit(llm_profiles=["llmb-temperature-fan"])).llm_profiles == [
        "llmb-temperature-fan"
    ]


@pytest.mark.parametrize(
    "values",
    [
        {"llm_profiles": ["Llmb"]},
        {"llm_profiles": ["-x"]},
        {"llm_profiles": ["a" * 81]},
        {"llm_profiles": "llmb-grade-prompt"},
        {"llm_profiles": ["p"] * 2},
        {"llm_profiles": [f"p{index}" for index in range(17)]},
        {"llm_profiles": ["other"], "completion": GRADING},
        {"completion": GRADING},
        {"llm_profiles": ["llmb-grade-prompt"], "completion": {**GRADING, "allow_skip": False}},
        {"llm_profiles": ["llmb-grade-prompt"], "completion": {**GRADING, "allow_fallback": False}},
        # The public completion kind is derived, never authored.
        {"llm_profiles": ["llmb-grade-prompt"], "completion": GRADING, "completion_kind": "llm-verdict"},
        {"completion_kind": "introduced"},
        {"llm_profiles": ["llmb-grade-prompt"], "completion": {**GRADING, "profile_sha256": "AB" * 32}},
        {"llm_profiles": ["llmb-grade-prompt"], "completion": {**GRADING, "rubric": "x"}},
        {"llm_profiles": ["llmb-grade-prompt"], "completion": {**GRADING, "answer": {"text": "x"}}},
        {"llm_profiles": ["llmb-grade-prompt"], "completion": GRADING, "room": "exercise"},
        {
            "llm_profiles": ["llmb-grade-prompt"],
            "completion": GRADING,
            "room": "video",
            "content": {lang: {"video": {"type": "youtube", "id": "aaaaaaaaaaa"}} for lang in ("de", "en")},
        },
        {"llm_grants": ["llmb-grade-prompt"]},
    ],
)
def test_invalid_llm_catalogue_entries_are_rejected(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CatalogueUnit.model_validate(unit(**values))
