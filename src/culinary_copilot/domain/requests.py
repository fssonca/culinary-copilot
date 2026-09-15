from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class CookingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingredients: list[NonEmptyText] = Field(default_factory=list, max_length=100)
    portions: int | None = Field(default=None, ge=1, le=100)
    time_minutes: int | None = Field(default=None, ge=1, le=1440)
    cuisine: NonEmptyText | None = None
    preferences: list[NonEmptyText] = Field(default_factory=list, max_length=50)
    dietary_constraints: list[NonEmptyText] = Field(default_factory=list, max_length=50)
    equipment: list[NonEmptyText] = Field(default_factory=list, max_length=50)
