"""The canonical JSON of this service is the lesson engine's, byte for byte.

The texts and hashes below were computed by the engine (`lesson-engine` `src/kernel/canonical.ts`, `digest`).
"""

from typing import Any

import pytest

from api.utils.canonical import CanonicalError, canonical, digest
from tests.engine_documents import document

VECTORS: list[tuple[Any, str, str]] = [
    (
        {
            "b": 1,
            "a": [True, False, None, -5, 0, 9007199254740991],
            "ä": 'x\n"\\\u0001\u001f\u007f',
            "z": {"😀": "  é", "￿": 2},
            "A": "tab\there",
            "": [],
        },
        '{"":[],"A":"tab\\u0009here","a":[true,false,null,-5,0,9007199254740991],"b":1,'
        '"z":{"￿":2,"😀":"  é"},"ä":"x\\u000a\\"\\\\\\u0001\\u001f\u007f"}',
        "8c167e590bd739a8e963c4f33b95192f99cf0015abeb258cb7f03889b52074f1",
    ),
    ("line\r\nend", '"line\\u000d\\u000aend"', "482cff87456d0a755f6c3674d7d084aca5d4e787770643f107c7df0b4c98f96f"),
    (
        [{}, [], "", -9007199254740991],
        '[{},[],"",-9007199254740991]',  # noqa: P103
        "fa776d2a0d6a443617ea3cdfa00652421fba36724f3ad9c3a34ead58ec7aa584",
    ),
]


@pytest.mark.parametrize("value, text, hashed", VECTORS)
def test_canonical_json_is_the_engines(value: Any, text: str, hashed: str) -> None:
    assert canonical(value) == text.encode("utf-8")
    assert digest(value) == hashed


def test_the_synthetic_server_document_has_the_engines_hash() -> None:
    # The same document hashed by the engine's `digest`.
    assert digest(document()) == "f48a401147b1c48c42c7ca250c22f37575e130198c134db0eea09a0340879f6c"


@pytest.mark.parametrize(
    "value", [1.5, 1.0, 2**53, -(2**53), "\ud800", {"a": float("nan")}, {1: "key"}, (1, 2), object()]
)
def test_values_without_a_canonical_form_are_refused(value: Any) -> None:
    with pytest.raises(CanonicalError):
        canonical(value)
