from pydantic import BaseModel, Field


class ClientAddRequest(BaseModel):
    uuid: str
    email: str
    expire_ms: int = Field(default=0)


class ClientRemoveRequest(BaseModel):
    uuid: str


class ConnLimitRequest(BaseModel):
    user_id: int
    # Per-user simultaneous-IP override. None clears the override (back to the
    # node default); 0 means unlimited; a positive value caps to that many IPs.
    limit: int | None = None
