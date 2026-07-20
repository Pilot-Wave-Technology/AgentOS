from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, EmailStr


class UserProfile(BaseModel):
    uid: str
    email: str
    display_name: str = ""
    photo_url: str = ""
    system_role: Literal["admin", "editor", "member"] = "member"
    status: Literal["active", "deactivated"] = "active"
    created_at: Optional[datetime] = None
