from typing import Annotated, Literal

from pydantic import BaseModel, Field


class DesiredClient(BaseModel):
    kind: Literal["client"]
    uuid: str
    email: str
    expire_ms: int = Field(ge=0)


class DesiredConnLimit(BaseModel):
    kind: Literal["conn_limit"]
    user_id: int
    limit: int = Field(ge=0)


DesiredItem = Annotated[
    DesiredClient | DesiredConnLimit,
    Field(discriminator="kind"),
]


class AppliedState(BaseModel):
    generation: int = Field(ge=0)
    digest: str | None = None
    # Persist the verified items as well as their digest so expiries can still
    # be enforced while every control endpoint is unreachable.
    items: list[DesiredItem] = Field(default_factory=list)


class SnapshotManifest(BaseModel):
    schema_version: Literal[1]
    generation: int = Field(ge=1)
    digest: str = Field(min_length=64, max_length=64)
    item_count: int = Field(ge=0)
    page_count: int = Field(ge=0)
    page_size: int = Field(ge=1)


class SnapshotPage(BaseModel):
    schema_version: Literal[1]
    generation: int = Field(ge=1)
    page_index: int = Field(ge=0)
    page_digest: str = Field(min_length=64, max_length=64)
    items: list[DesiredItem]


class DesiredSnapshot(BaseModel):
    generation: int = Field(ge=1)
    digest: str = Field(min_length=64, max_length=64)
    items: list[DesiredItem]
