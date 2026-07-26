from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class DesiredClient(BaseModel):
    kind: Literal["client"] = "client"
    uuid: str
    email: str
    expire_ms: int = Field(ge=0)


class DesiredConnLimit(BaseModel):
    kind: Literal["conn_limit"] = "conn_limit"
    user_id: int
    limit: int = Field(ge=0)


class DesiredCascadeService(BaseModel):
    kind: Literal["cascade_service"] = "cascade_service"
    route_id: int = Field(ge=1)
    revision: int = Field(ge=1)
    config_digest: str = Field(min_length=64, max_length=64)
    uuid: str
    email: str


class DesiredCascadeExit(BaseModel):
    position: int = Field(ge=0)
    host: str
    port: int = Field(ge=1, le=65535)
    uuid: str
    public_key: str
    short_id: str
    server_name: str
    xhttp_path: str = "/"


class DesiredCascadeRoute(BaseModel):
    kind: Literal["cascade_route"] = "cascade_route"
    route_id: int = Field(ge=1)
    revision: int = Field(ge=1)
    config_digest: str = Field(min_length=64, max_length=64)
    label: str
    inbound_tags: list[str] = Field(min_length=1)
    exits: list[DesiredCascadeExit] = Field(min_length=1)
    health_policy: dict = Field(default_factory=dict)


DesiredItem = Annotated[
    DesiredClient | DesiredConnLimit | DesiredCascadeService | DesiredCascadeRoute,
    Field(discriminator="kind"),
]


class SnapshotManifest(BaseModel):
    schema_version: Literal[1, 2] = 1
    generation: int
    digest: str
    item_count: int
    page_count: int
    page_size: int


class SnapshotPage(BaseModel):
    schema_version: Literal[1, 2] = 1
    generation: int
    page_index: int
    page_digest: str
    items: list[DesiredItem]


class NodeSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied_generation: int = Field(ge=0)
    applied_digest: str | None = Field(default=None, max_length=64)
    agent_version: str = Field(max_length=64)
    capabilities: list[str] = Field(default_factory=list, max_length=64)


class NodeAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation: int = Field(ge=1)
    digest: str = Field(min_length=64, max_length=64)
    success: bool
    error: str | None = Field(default=None, max_length=512)


class NodeTelemetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=0)
    payload: dict


class NodeControlResult(BaseModel):
    status: Literal["ok", "duplicate", "error-recorded"]
