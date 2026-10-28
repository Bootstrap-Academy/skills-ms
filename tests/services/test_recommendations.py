"""Suggestions for the learning plan: a pure function, deterministic, without a model and without storage."""

import random
from typing import Any

import pytest

from api.services.recommendations import LIMIT, Learner, SkillGraph, root_depths, suggest

# start ── os ──┐
#    │  └─ net ─┴─ sec        web depends on prog, prog on start
#    └─ prog ── web
ROOTS = {"start": [], "os": ["start"], "net": ["start"], "sec": ["os", "net"], "prog": ["start"], "web": ["prog"]}
SUB_SKILLS = {
    "it": ("start", []),
    "media": ("start", []),
    "linux": ("os", []),
    "bash": ("os", ["linux"]),
    "network": ("net", []),
    "security": ("sec", []),
    "python": ("prog", []),
    "html": ("web", []),
    "js": ("web", ["html"]),
}
COURSES = {f"c-{skill}": skill for skill in SUB_SKILLS}
# Only one course carries a level; the others take the depth of their root.
LEVELS: dict[str, str | None] = {course: None for course in COURSES} | {"c-html": "einsteiger"}
TRACKS = {"web-track": ["c-html", "c-js"], "other": ["c-security"]}


def graph() -> SkillGraph:
    return SkillGraph(
        root_dependencies={root: frozenset(deps) for root, deps in ROOTS.items()},
        sub_skill_roots={skill: root for skill, (root, _) in SUB_SKILLS.items()},
        sub_skill_dependencies={skill: frozenset(deps) for skill, (_, deps) in SUB_SKILLS.items()},
        course_skills={course: frozenset([skill]) for course, skill in COURSES.items()},
    )


def learner(**values: Any) -> Learner:
    defaults: dict[str, Any] = {
        "character": None,
        "preference": None,
        "planned": [],
        "tracks": [],
        "xp_skills": frozenset(),
        "completed": frozenset(),
    }
    return Learner(**(defaults | values))


def summary(found: list[Any]) -> list[tuple[str, str, int]]:
    return [(item.course, item.reason, item.score) for item in found]


def test_root_depth_is_the_longest_chain_and_survives_a_cycle() -> None:
    assert root_depths({root: frozenset(deps) for root, deps in ROOTS.items()}) == {
        "start": 0,
        "os": 1,
        "net": 1,
        "prog": 1,
        "sec": 2,
        "web": 2,
    }
    cyclic = root_depths({"a": frozenset({"b"}), "b": frozenset({"a"}), "c": frozenset({"c"})})
    assert cyclic == {"a": 1, "b": 0, "c": 0}


def test_a_new_learner_starts_with_new_areas_of_the_first_level() -> None:
    # Without a choice the function reckons with einsteiger and breit, as the plan page starts.
    found = suggest(graph(), learner(), LEVELS, TRACKS)
    assert summary(found) == [
        ("c-it", "new_area", 4),
        ("c-media", "new_area", 4),
        # Its own level (einsteiger) overrides the depth of its root (2, profi).
        ("c-html", "level_fit", 2),
        ("c-linux", "level_fit", 0),
        ("c-network", "level_fit", 0),
        ("c-python", "level_fit", 0),
    ]
    assert found == suggest(graph(), learner(character="einsteiger", preference="breit"), LEVELS, TRACKS)


def test_a_finished_course_leads_to_the_next_steps_whose_prerequisites_are_met() -> None:
    found = suggest(graph(), learner(completed=frozenset({"c-it"})), LEVELS, TRACKS)
    assert summary(found) == [
        ("c-linux", "next_after", 6),
        ("c-network", "next_after", 6),
        ("c-python", "next_after", 6),
        ("c-html", "level_fit", 2),
        ("c-media", "level_fit", 2),
        # Bash needs Linux first: no next step, fewer points.
        ("c-bash", "level_fit", -2),
    ]
    assert [item.reason_course for item in found[:3]] == ["c-it", "c-it", "c-it"]
    assert all(item.reason_course is None and item.reason_track is None for item in found[3:])


def test_deep_preference_stays_in_the_areas_of_the_plan_and_the_xp() -> None:
    found = suggest(
        graph(), learner(preference="tief", planned=["c-linux"], xp_skills=frozenset({"network"})), LEVELS, TRACKS
    )
    assert summary(found) == [
        # The planned Linux course covers what Bash needs.
        ("c-bash", "next_after", 6),
        ("c-html", "level_fit", 2),
        ("c-it", "level_fit", 2),
        ("c-media", "level_fit", 2),
        ("c-network", "deepen", 2),
        # Security needs os (planned) and net (XP); it is profi for an einsteiger.
        ("c-security", "next_after", 1),
    ]


def test_character_moves_suggestions_to_its_level() -> None:
    found = suggest(graph(), learner(character="profi", completed=frozenset({"c-python", "c-html"})), LEVELS, TRACKS)
    assert summary(found)[:2] == [("c-js", "next_after", 6), ("c-security", "level_fit", 2)]


def test_courses_of_a_track_in_the_plan_are_suggested_with_the_track() -> None:
    found = suggest(graph(), learner(planned=["c-it"], tracks=["web-track"]), LEVELS, TRACKS)
    by_course = {item.course: item for item in found}
    assert (by_course["c-html"].reason, by_course["c-html"].reason_track, by_course["c-html"].score) == (
        "in_track",
        "web-track",
        5,
    )
    assert "c-security" not in by_course


def test_done_planned_and_unknown_courses_are_never_suggested_and_at_most_six() -> None:
    levels = {course: level for course, level in LEVELS.items() if course != "c-media"}
    everyone = frozenset(COURSES) - {"c-js", "c-security"}
    found = suggest(graph(), learner(planned=["c-js"], completed=everyone - {"c-js"}), levels, TRACKS)
    assert [item.course for item in found] == ["c-security"]
    for case in [learner(), learner(completed=frozenset({"c-it"})), learner(planned=["c-linux", "c-bash"])]:
        found = suggest(graph(), case, LEVELS, TRACKS)
        assert len(found) <= LIMIT == 6
        assert not {item.course for item in found} & (set(case.planned) | case.completed)


@pytest.mark.parametrize("seed", range(5))
def test_the_order_of_the_inputs_does_not_change_the_result(seed: int) -> None:
    case = learner(preference="tief", planned=["c-linux", "c-html"], completed=frozenset({"c-it"}))
    expected = suggest(graph(), case, LEVELS, TRACKS)
    shuffled = random.Random(seed)  # noqa: S311

    def mixed(mapping: dict[str, Any]) -> dict[str, Any]:
        items = list(mapping.items())
        shuffled.shuffle(items)
        return dict(items)

    original = graph()
    reordered = SkillGraph(
        root_dependencies=mixed(dict(original.root_dependencies)),
        sub_skill_roots=mixed(dict(original.sub_skill_roots)),
        sub_skill_dependencies=mixed(dict(original.sub_skill_dependencies)),
        course_skills=mixed(dict(original.course_skills)),
    )
    assert suggest(reordered, case, mixed(LEVELS), mixed(TRACKS)) == expected
