"""Suggestions of courses for a learner's plan: a pure, deterministic function, without a model and without storage.

Candidates are the courses of the skill tree (`SkillCourse`) that exist and are neither done nor planned. A sub
skill is covered when the learner has XP in it or a done or planned course teaches it; a course is ready when every
sub skill its own sub skills depend on is covered. Each candidate gets points:

- level fit: the course's `level`, else the depth of its root skill in the tree (longest chain of root dependencies:
  0 einsteiger, 1 fortgeschritten, 2 or more profi), against the learner's character (einsteiger if none is chosen);
- next step: a ready course whose sub skill depends on a sub skill of a planned or done course, or whose root depends
  on that course's root while every root it depends on has progress or a planned course;
- track: a course of a track the plan takes steps from;
- preference ("breit" if none is chosen): "breit" favours a ready course in a root without progress whose required
  roots all have XP or a done course, "tief" a course in a root with XP, a done or a planned course;
- a course that is not ready loses points.

The order is points, then course ID. Every suggestion names its strongest reason as a text key: next step, track,
preference, else level fit.
"""

from dataclasses import dataclass
from typing import Mapping, Sequence

from api.schemas.course import LEVELS
from api.schemas.learning_plan import Reason

RANK: dict[str, int] = {level: rank for rank, level in enumerate(LEVELS)}
LIMIT = 6
NEXT_AFTER = 4
IN_TRACK = 3
PREFERENCE = 2
NOT_READY = -2
# By the distance between the course's level and the character, in steps of the three levels.
LEVEL_FIT = {0: 2, 1: 0, 2: -3}


@dataclass(frozen=True)
class SkillGraph:
    root_dependencies: Mapping[str, frozenset[str]]
    sub_skill_roots: Mapping[str, str]
    sub_skill_dependencies: Mapping[str, frozenset[str]]
    course_skills: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class Learner:
    character: str | None
    preference: str | None
    planned: Sequence[str]  # course IDs in the order of the plan
    tracks: Sequence[str]  # track IDs the plan takes steps from, in the order of the plan
    xp_skills: frozenset[str]  # sub skills with XP
    completed: frozenset[str]  # courses done by their completion rules


@dataclass(frozen=True)
class Suggestion:
    course: str
    reason: Reason
    score: int
    reason_course: str | None = None
    reason_track: str | None = None


def root_depths(dependencies: Mapping[str, frozenset[str]]) -> dict[str, int]:
    """The longest chain of dependencies below each root; an edge that would close a cycle is ignored."""
    depths: dict[str, int] = {}

    def depth(root: str, visiting: frozenset[str]) -> int:
        if root not in depths:
            below = [
                depth(dependency, visiting | {root})
                for dependency in sorted(dependencies.get(root, frozenset()))
                if dependency not in visiting and dependency != root
            ]
            depths[root] = 1 + max(below) if below else 0
        return depths[root]

    for root in sorted(dependencies):
        depth(root, frozenset())
    return depths


class _Tree:
    """The graph seen from one learner."""

    def __init__(self, graph: SkillGraph, learner: Learner, levels: Mapping[str, str | None]) -> None:
        self.graph = graph
        self.levels = levels
        self.depths = root_depths(graph.root_dependencies)
        planned = set(learner.planned)
        xp_roots = {graph.sub_skill_roots[skill] for skill in learner.xp_skills if skill in graph.sub_skill_roots}
        done_roots = {root for course in learner.completed for root in self.roots(course)}
        self.plan_roots = {root for course in planned for root in self.roots(course)}
        # Roots with progress: XP or a done course.
        self.satisfied = xp_roots | done_roots
        self.known = self.satisfied | self.plan_roots
        self.covered = learner.xp_skills.union(*(self.skills(course) for course in sorted(learner.completed | planned)))

    def skills(self, course: str) -> frozenset[str]:
        return self.graph.course_skills.get(course, frozenset())

    def roots(self, course: str) -> set[str]:
        return {
            self.graph.sub_skill_roots[skill] for skill in self.skills(course) if skill in self.graph.sub_skill_roots
        }

    def requires(self, root: str) -> frozenset[str]:
        return self.graph.root_dependencies.get(root, frozenset())

    def level(self, course: str) -> int | None:
        explicit = RANK.get(self.levels.get(course) or "")
        if explicit is not None:
            return explicit
        found = self.roots(course)
        return min(min(self.depths.get(root, 0), 2) for root in found) if found else None

    def ready(self, course: str) -> bool:
        return all(
            self.graph.sub_skill_dependencies.get(skill, frozenset()) <= self.covered for skill in self.skills(course)
        )

    def follows(self, course: str, anchor: str) -> bool:
        before = self.skills(anchor)
        if any(self.graph.sub_skill_dependencies.get(skill, frozenset()) & before for skill in self.skills(course)):
            return True
        anchor_roots = self.roots(anchor)
        return any(
            self.requires(root) & anchor_roots and self.requires(root) <= self.satisfied | self.plan_roots
            for root in self.roots(course)
        )


def suggest(
    graph: SkillGraph,
    learner: Learner,
    levels: Mapping[str, str | None],
    tracks: Mapping[str, Sequence[str]],
    limit: int = LIMIT,
) -> list[Suggestion]:
    """Up to `limit` suggestions. `levels` holds every existing course with its explicit level or None, `tracks` the
    courses of every track."""
    tree = _Tree(graph, learner, levels)
    character = RANK.get(learner.character or "", 0)
    preference = learner.preference or "breit"
    planned = set(learner.planned)
    anchors = [*dict.fromkeys(learner.planned), *sorted(learner.completed - planned)]
    suggestions = []
    for course in sorted(graph.course_skills):
        if course not in levels or course in planned or course in learner.completed:
            continue
        ready = tree.ready(course)
        own = tree.roots(course)
        course_level = tree.level(course)
        after = (
            next((anchor for anchor in anchors if anchor != course and tree.follows(course, anchor)), None)
            if ready
            else None
        )
        track = next((track for track in learner.tracks if course in tracks.get(track, ())), None)
        new_area = (
            preference == "breit"
            and ready
            and any(root not in tree.known and tree.requires(root) <= tree.satisfied for root in own)
        )
        deepen = preference == "tief" and bool(own & tree.known)
        score = (
            (0 if course_level is None else LEVEL_FIT[abs(course_level - character)])
            + NEXT_AFTER * (after is not None)
            + IN_TRACK * (track is not None)
            + PREFERENCE * (new_area or deepen)
            + NOT_READY * (not ready)
        )
        if after is not None:
            suggestions.append(Suggestion(course, "next_after", score, reason_course=after))
        elif track is not None:
            suggestions.append(Suggestion(course, "in_track", score, reason_track=track))
        else:
            reason: Reason = "new_area" if new_area else "deepen" if deepen else "level_fit"
            suggestions.append(Suggestion(course, reason, score))
    suggestions.sort(key=lambda suggestion: (-suggestion.score, suggestion.course))
    return suggestions[:limit]
