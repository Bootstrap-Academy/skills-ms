"""The small public contract for trusted, independently published browser modules."""

from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field, validator


MODULE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,79}$"


class LessonModuleDescriptor(BaseModel):
    id: str = Field(regex=MODULE_ID_PATTERN)
    api_version: Literal[1]
    entry_url: str = Field(max_length=2048)

    class Config:
        extra = "forbid"

    @validator("api_version", pre=True)
    @classmethod
    def exact_version(cls, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool) or value != 1:
            raise ValueError("Only module API version 1 is supported")
        return value

    @validator("entry_url")
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
