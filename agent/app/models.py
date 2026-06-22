from pydantic import BaseModel, ConfigDict, Field


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


class Hy2AuthRequest(BaseModel):
    # Body Hysteria2 POSTs to /hy2/auth on every new connection. We only need
    # `auth` (the client's xray UUID, used as the Hy2 secret); addr/tx are
    # informational. Tolerant of missing/extra fields.
    model_config = ConfigDict(extra="ignore")

    addr: str = ""
    auth: str = ""
    tx: int = 0
