from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime


# ---------- USER ----------
class UserIn(BaseModel):
    telegram_id: int
    name: str
    state: str
    district: str
    language: Literal["en", "hi"] = "en"


class UserOut(UserIn):
    debates_hosted: int = 0
    host_rating_avg: float = 0.0
    host_rating_count: int = 0
    created_at: datetime


# ---------- TOPIC ----------
class TopicIn(BaseModel):
    title_en: str
    title_hi: Optional[str] = None
    state: str
    district: Optional[str] = None  # None = state-wide topic
    created_by: int  # telegram_id of admin or suggesting user


class TopicOut(TopicIn):
    id: str
    status: Literal["pending", "approved", "rejected"] = "pending"
    created_at: datetime


class TopicApprove(BaseModel):
    approve: bool  # true = approve, false = reject


# ---------- ROOM ----------
class RoomCreate(BaseModel):
    topic_id: str
    host_telegram_id: int


class RoomJoin(BaseModel):
    telegram_id: int
    side: Literal["pro", "con"]


class RoomOut(BaseModel):
    id: str
    topic_id: str
    host_telegram_id: int
    pro_seats: list[int] = []
    con_seats: list[int] = []
    status: Literal["waiting", "full", "live", "ended"] = "waiting"
    created_at: datetime


# ---------- REVIEW (post-debate feedback) ----------
class ReviewIn(BaseModel):
    room_id: str
    telegram_id: int  # who is submitting the review
    host_rating: int = Field(ge=1, le=5)
    final_opinion: str


# ---------- APP SETTINGS (admin-controlled) ----------
class AppSettings(BaseModel):
    home_bg: Optional[str] = None
    messages_bg: Optional[str] = None
    account_bg: Optional[str] = None
    anthem_audio_url: Optional[str] = None  # plays for everyone when a host starts a debate


# ---------- SPECTATORS & PUBLIC CHAT ----------
class SpectateIn(BaseModel):
    telegram_id: int
    name: str = "Guest"


class ChatMessageIn(BaseModel):
    telegram_id: int
    name: str
    text: str = Field(min_length=1, max_length=300)


# ---------- SCHEDULING & STARTING A DEBATE ----------
class ScheduleIn(BaseModel):
    host_telegram_id: int
    scheduled_time: datetime  # ISO datetime the host picked


class StartIn(BaseModel):
    host_telegram_id: int


# ---------- MODERATION ----------
class BanIn(BaseModel):
    telegram_id: int
    ban_type: Literal["temporary", "permanent"]
    hours: Optional[int] = None  # required if ban_type == "temporary"
    reason: Optional[str] = None


class WarnIn(BaseModel):
    message: str
