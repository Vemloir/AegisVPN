from pydantic import BaseModel, Field

class ClientAddRequest(BaseModel):
    uuid: str
    email: str
    expire_ms: int = Field(default=0)

class ClientRemoveRequest(BaseModel):
    uuid: str
