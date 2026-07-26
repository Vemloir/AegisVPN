from typing import Annotated, Literal

from pydantic import BaseModel, Field


class DesiredClient(BaseModel):
    kind: Literal["client"] = "client"
    uuid: str
    email: str
    expire_ms: int = Field(ge=0)


class DesiredConnLimit(BaseModel):
    kind: Literal["conn_limit"] = "conn_limit"
    user_id: int
    limit: int = Field(ge=0)


DesiredItem = Annotated[
    DesiredClient | DesiredConnLimit,
    Field(discriminator="kind"),
]


class SnapshotManifest(BaseModel):
    schema_version: int = 1
    generation: int
    digest: str
    item_count: int
    page_count: int
    page_size: int


class SnapshotPage(BaseModel):
    schema_version: int = 1
    generation: int
    page_index: int
    page_digest: str
    items: list[DesiredItem]
