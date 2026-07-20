from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel


class Message(BaseModel):
    id: str
    thread_id: str
    sender: str               # uid or agent_id
    role: Literal["human", "agent"] = "human"
    content: str
    timestamp: Optional[datetime] = None
    token_count: int = 0
    attachments: list[str] = []   # GCS URIs


class Thread(BaseModel):
    id: str
    goal_id: str
    agent_id: str
    created_by: str
    created_at: Optional[datetime] = None
    title: str = "New conversation"
