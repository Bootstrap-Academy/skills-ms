"""Canonical JSON as the lesson engine writes it, and its SHA-256.

The engine hashes a server document over these bytes (`documentHash`), so this service must produce the very
same: compact, object keys sorted by Unicode code point, arrays in order, safe integers only, and in strings
only quote, backslash and control characters escaped, the latter always as lowercase \\u00xx (unlike
`json.dumps`, which writes \\n and its relatives). UTF-8, no lone surrogates.
"""

from hashlib import sha256
from typing import Any

SAFE_INTEGER = 2**53 - 1


class CanonicalError(ValueError):
    pass


def _text(value: str) -> str:
    out = ['"']
    for char in value:
        code = ord(char)
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:
            raise CanonicalError("lone surrogate in a string")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if not -SAFE_INTEGER <= value <= SAFE_INTEGER:
            raise CanonicalError(f"{value} is no safe integer")
        out.append(str(value))
    elif isinstance(value, str):
        out.append(_text(value))
    elif isinstance(value, list):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        for index, key in enumerate(sorted(value)):
            if not isinstance(key, str):
                raise CanonicalError("object key is no string")
            if index:
                out.append(",")
            out.append(_text(key))
            out.append(":")
            _write(value[key], out)
        out.append("}")
    else:
        # Floats included: the engine has integers only.
        raise CanonicalError(f"{type(value).__name__} has no canonical form")


def canonical(value: Any) -> bytes:
    """The canonical JSON bytes of a value. Raises CanonicalError for anything without a canonical form."""
    out: list[str] = []
    _write(value, out)
    return "".join(out).encode("utf-8")


def digest(value: Any) -> str:
    """SHA-256 over the canonical JSON bytes, as 64 lowercase hex digits (the engine's `digest`)."""
    return sha256(canonical(value)).hexdigest()
