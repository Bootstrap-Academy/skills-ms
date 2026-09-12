"""Validate the shipped catalogue independently of synthetic route fixtures."""

from api.schemas.rooms import Unit
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
            assert "completion" not in public.dict() and "retired" not in public.dict()
            if unit.room != "exercise":
                assert unit.completion is not None and unit.completion.answer
