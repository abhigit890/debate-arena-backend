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
    photo_url: Optional[str] = None


class UserOut(UserIn):
    debates_hosted: int = 0
    host_rating_avg: float = 0.0
    host_rating_count: int = 0
    created_at: datetime


# ---------- ROOM ----------
class RoomCreate(BaseModel):
    host_telegram_id: int
    topic_title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=1, max_length=1000)  # the host's own take on the topic
    questions: list[str] = Field(min_length=3)  # asked to anyone applying to attend as a guest


class RoomJoin(BaseModel):
    telegram_id: int
    side: Literal["pro", "con"]


class RoomOut(BaseModel):
    id: str
    topic_title: str
    description: str
    questions: list[str]
    host_telegram_id: int
    pro_seats: list[int] = []
    con_seats: list[int] = []
    guests: list[dict] = []
    status: Literal["waiting", "full", "anthem", "live", "ended"] = "waiting"
    created_at: datetime


# ---------- GUEST APPLICATIONS ----------
class GuestApplyIn(BaseModel):
    telegram_id: int
    name: str
    answers: list[str]


class GuestReviewIn(BaseModel):
    host_telegram_id: int
    approve: bool


# ---------- ROOM BACKGROUND PURCHASE (paid with Revolution Coins) ----------
class SetRoomBackgroundIn(BaseModel):
    host_telegram_id: int
    image_base64: str


# ---------- DIRECT MESSAGES ----------
class SendMessageIn(BaseModel):
    from_id: int
    to_id: int
    text: str = Field(min_length=1, max_length=1000)


# ---------- LEVEL TIERS (admin-configurable decorations) ----------
class LevelTierIn(BaseModel):
    level: int = Field(ge=1)
    min_score: int = Field(ge=0)  # debates_hosted*10 + coins must reach this to unlock the level
    frame_image: Optional[str] = None  # decorative border around the profile photo
    badge_text: Optional[str] = None   # e.g. "Legend"


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
    active_rooms_bg: Optional[str] = None
    upcoming_rooms_bg: Optional[str] = None
    logo_image: Optional[str] = None
    anthem_audio_url: Optional[str] = None  # plays for everyone when a host starts a debate
    host_questions: Optional[list[str]] = None  # asked to anyone applying to become a host


# ---------- HOST APPLICATIONS ----------
class HostApplicationIn(BaseModel):
    telegram_id: int
    name: str
    answers: list[str]
    whatsapp_number: str = Field(min_length=8, max_length=20)  # so an admin can call/message to verify


class HostReviewIn(BaseModel):
    approve: bool


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
