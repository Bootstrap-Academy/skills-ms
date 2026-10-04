"""Validate the shipped catalogue independently of synthetic route fixtures."""

from api.schemas.rooms import IntroductionCompletion, Unit
from api.services.rooms import load_catalogue


def test_shipped_paths_have_reachable_prerequisites_and_private_checks() -> None:
    content = load_catalogue()
    units = {unit.id: unit for unit in content.units}
    for path in content.paths:
        concepts: set[str] = set()
        for unit_id in path.units:
            unit = units[unit_id]
            assert set(unit.requires).issubset(concepts)
            concepts.update([*unit.teaches, *unit.practices])
            public = unit.public()
            assert isinstance(public, Unit)
            assert "completion" not in public.model_dump() and "retired" not in public.model_dump()
            assert public.completion_kind == (None if unit.completion is None else unit.completion.kind)
            if unit.room != "exercise":
                assert isinstance(unit.completion, IntroductionCompletion) and unit.completion.answer
