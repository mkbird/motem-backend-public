from pydantic import BaseModel


class ChatRequest(BaseModel):
    text: str
    owner_id: str
    device_id: str = "share_extension"


class ChatResponse(BaseModel):
    text: str
