"""The small public contract for trusted, independently published browser modules."""

from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, field_validator

from api.schemas import BaseModel

MODULE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,79}$"


class LessonModuleDescriptor(BaseModel):
    id: str = Field(pattern=MODULE_ID_PATTERN)
    api_version: Literal[1]
    entry_url: str = Field(max_length=2048)

    @field_validator("api_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool) or value != 1:
            raise ValueError("Only module API version 1 is supported")
        return value

    @field_validator("entry_url")
    @classmethod
    def browser_module_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            parsed.port
        except ValueError:
            raise ValueError("Invalid module URL port") from None
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or any(part in (".", "..") for part in unquote(parsed.path).split("/"))
            or not parsed.path.endswith((".mjs", ".js"))
        ):
            raise ValueError("A canonical absolute browser-module URL is required")
        return value


class EnginePackageDescriptor(BaseModel):
    """A package of the lesson engine (protocol 2.1), as the frontend's `PackageDescriptor` reads it.

    The bytes lie under `{content origin}/packages/{package_hash}/`; the frontend checks both hashes and the
    location before it loads anything. `revision_id` names the server side that evaluates this package.
    """

    id: str = Field(pattern=MODULE_ID_PATTERN)
    api_version: Literal[2]
    entry_url: str = Field(max_length=2048)
    manifest_url: str = Field(max_length=2048)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision_id: str = Field(pattern=r"^[0-9a-f-]{36}$")


ModuleDescriptor = LessonModuleDescriptor | EnginePackageDescriptor
