"""Shared validation defaults for the service's wire contracts."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel as PydanticBaseModel
from pydantic import ConfigDict, PlainSerializer, WithJsonSchema


class BaseModel(PydanticBaseModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)


# Keep the service's existing ISO 8601 representation, including +00:00 for UTC.
Timestamp = Annotated[
    datetime,
    PlainSerializer(datetime.isoformat, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}, mode="serialization"),
]
