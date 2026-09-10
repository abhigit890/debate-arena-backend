from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
from typing import Optional
from bson import ObjectId
import os

from database import (
    users_col, topics_col, rooms_col, reviews_col, settings_col, bans_col,
    room_messages_col, ensure_indexes
)
from models import (
    UserIn, TopicIn, TopicApprove, RoomCreate, RoomJoin, ReviewIn,
    AppSettings, BanIn, WarnIn, SpectateIn, ChatMessageIn, ScheduleIn, StartIn
)

app = FastAPI(title="Debate Arena API")

ADMIN_KEY = os.getenv("ADMIN_KEY", "")  # set this on Render; admin.html sends it back

# Allow requests from your Telegram Mini App (hosted on Vercel)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this to your Vercel URL once live
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_admin(x_admin_key: str = Header(default="")):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(401, "Admin key missing or incorrect")


@app.on_event("startup")
def startup():
    ensure_indexes()


def oid(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(400, "Invalid id")


# ============ USERS ============

@app.post("/users")
def upsert_user(user: UserIn):
    """Create a user on first Mini App open, or update state/district/language."""
    users_col.update_one(
        {"telegram_id": user.telegram_id},
        {
            "$set": user.model_dump(),
            "$setOnInsert": {
                "debates_hosted": 0,
                "host_rating_avg": 0.0,
                "host_rating_count": 0,
                "created_at": datetime.utcnow(),
            },
        },
        upsert=True,
    )
    return {"ok": True}


@app.get("/users/{telegram_id}")
def get_user(telegram_id: int):
    user = users_col.find_one({"telegram_id": telegram_id})
    if not user:
        raise HTTPException(404, "User not found")
    user["_id"] = str(user["_id"])
    return user


# ============ TOPICS ============

@app.post("/topics")
def create_topic(topic: TopicIn, is_admin: bool = False):
    """
    Admin-added topics go straight to 'approved'.
    User-suggested topics go to 'pending' until an admin approves them.
    """
    doc = topic.model_dump()
    doc["status"] = "approved" if is_admin else "pending"
    doc["created_at"] = datetime.utcnow()
    result = topics_col.insert_one(doc)
    return {"id": str(result.inserted_id), "status": doc["status"]}


@app.get("/topics")
def list_topics(state: str, district: str | None = None, status: str = "approved"):
    """
    Returns topics for a state. If district is given, district-specific
    topics are returned first, followed by state-wide topics.
    """
    query = {"state": state, "status": status}
    topics = list(topics_col.find(query))
    for t in topics:
        t["id"] = str(t.pop("_id"))

    if district:
        topics.sort(key=lambda t: 0 if t.get("district") == district else 1)

    return topics


@app.post("/topics/{topic_id}/review")
def review_topic(topic_id: str, decision: TopicApprove):
    """Admin approves or rejects a user-suggested topic."""
    new_status = "approved" if decision.approve else "rejected"
    result = topics_col.update_one(
        {"_id": oid(topic_id)}, {"$set": {"status": new_status}}
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Topic not found")
    return {"ok": True, "status": new_status}


# ============ ROOMS ============

@app.get("/rooms")
def list_rooms(topic_id: str, status: str = "waiting,full,anthem,live"):
    """Used by the Mini App to find any active room for a topic (comma-separated statuses)."""
    statuses = [s.strip() for s in status.split(",")]
    rooms = list(rooms_col.find({"topic_id": topic_id, "status": {"$in": statuses}}).sort("created_at", -1))
    for r in rooms:
        r["id"] = str(r.pop("_id"))
    return rooms


@app.post("/rooms")
def create_room(room: RoomCreate):
    topic = topics_col.find_one({"_id": oid(room.topic_id)})
    if not topic:
        raise HTTPException(404, "Topic not found")

    doc = {
        "topic_id": room.topic_id,
        "host_telegram_id": room.host_telegram_id,
        "pro_seats": [],
        "con_seats": [],
        "status": "waiting",
        "viewers": [],  # telegram_ids currently spectating (not seated)
        "scheduled_time": None,
        "anthem_started_at": None,
        "created_at": datetime.utcnow(),
    }
    result = rooms_col.insert_one(doc)
    return {"id": str(result.inserted_id)}


@app.post("/rooms/{room_id}/join")
def join_room(room_id: str, join: RoomJoin):
    active_ban = bans_col.find_one({
        "telegram_id": join.telegram_id,
        "$or": [{"ban_type": "permanent"}, {"until": {"$gt": datetime.utcnow()}}],
    })
    if active_ban:
        raise HTTPException(403, "You are banned from joining rooms.")

    room = rooms_col.find_one({"_id": oid(room_id)})
    if not room:
        raise HTTPException(404, "Room not found")
    if room["status"] != "waiting":
        raise HTTPException(400, "Room is not accepting new members")

    side_key = "pro_seats" if join.side == "pro" else "con_seats"
    if len(room[side_key]) >= 4:
        raise HTTPException(400, f"{join.side} side is full")
    if join.telegram_id in room["pro_seats"] + room["con_seats"]:
        raise HTTPException(400, "You already joined this room")

    rooms_col.update_one({"_id": oid(room_id)}, {"$push": {side_key: join.telegram_id}})

    # Re-check: if both sides now have 4/4, mark room full and signal "ready to notify"
    updated = rooms_col.find_one({"_id": oid(room_id)})
    is_full = len(updated["pro_seats"]) == 4 and len(updated["con_seats"]) == 4
    if is_full:
        rooms_col.update_one({"_id": oid(room_id)}, {"$set": {"status": "full"}})
        # NOTE: your Telegram bot process should poll for status == "full"
        # (or you can call the Telegram Bot API directly from here later)
        # to message the host + all 8 participants that the room is ready.

    return {"ok": True, "room_full": is_full}


@app.get("/rooms/{room_id}")
def get_room(room_id: str):
    room = rooms_col.find_one({"_id": oid(room_id)})
    if not room:
        raise HTTPException(404, "Room not found")
    room["id"] = str(room.pop("_id"))
    return room


@app.post("/rooms/{room_id}/end")
def end_room(room_id: str):
    room = rooms_col.find_one({"_id": oid(room_id)})
    if not room:
        raise HTTPException(404, "Room not found")

    rooms_col.update_one({"_id": oid(room_id)}, {"$set": {"status": "ended"}})
    users_col.update_one(
        {"telegram_id": room["host_telegram_id"]}, {"$inc": {"debates_hosted": 1}}
    )
    return {"ok": True}


# ============ SPECTATORS (anyone can watch + public-chat, without a seat) ============

@app.post("/rooms/{room_id}/spectate")
def start_spectating(room_id: str, spec: SpectateIn):
    room = rooms_col.find_one({"_id": oid(room_id)})
    if not room:
        raise HTTPException(404, "Room not found")
    # Seated members (host or one of the 8) aren't counted as separate viewers.
    if spec.telegram_id in [room["host_telegram_id"]] + room["pro_seats"] + room["con_seats"]:
        return {"ok": True, "viewer_count": len(room.get("viewers", []))}

    rooms_col.update_one({"_id": oid(room_id)}, {"$addToSet": {"viewers": spec.telegram_id}})
    updated = rooms_col.find_one({"_id": oid(room_id)})
    return {"ok": True, "viewer_count": len(updated.get("viewers", []))}


@app.post("/rooms/{room_id}/unspectate")
def stop_spectating(room_id: str, spec: SpectateIn):
    rooms_col.update_one({"_id": oid(room_id)}, {"$pull": {"viewers": spec.telegram_id}})
    updated = rooms_col.find_one({"_id": oid(room_id)})
    if not updated:
        raise HTTPException(404, "Room not found")
    return {"ok": True, "viewer_count": len(updated.get("viewers", []))}


# ============ PUBLIC ROOM CHAT (spectators + seated members) ============

@app.post("/rooms/{room_id}/chat")
def post_chat_message(room_id: str, msg: ChatMessageIn):
    if not rooms_col.find_one({"_id": oid(room_id)}):
        raise HTTPException(404, "Room not found")
    doc = {
        "room_id": room_id,
        "telegram_id": msg.telegram_id,
        "name": msg.name,
        "text": msg.text,
        "created_at": datetime.utcnow(),
    }
    room_messages_col.insert_one(doc)
    return {"ok": True}


@app.get("/rooms/{room_id}/chat")
def get_chat_messages(room_id: str, after: Optional[str] = None):
    """Poll this every few seconds. Pass the last message's id in `after` to fetch only new ones."""
    query = {"room_id": room_id}
    if after:
        query["_id"] = {"$gt": oid(after)}
    messages = list(room_messages_col.find(query).sort("_id", 1).limit(200))
    for m in messages:
        m["id"] = str(m.pop("_id"))
    return messages


# ============ SCHEDULING & STARTING (national anthem gate) ============

@app.post("/rooms/{room_id}/schedule")
def schedule_room(room_id: str, sched: ScheduleIn):
    room = rooms_col.find_one({"_id": oid(room_id)})
    if not room:
        raise HTTPException(404, "Room not found")
    if room["host_telegram_id"] != sched.host_telegram_id:
        raise HTTPException(403, "Only the host can schedule this room")
    rooms_col.update_one({"_id": oid(room_id)}, {"$set": {"scheduled_time": sched.scheduled_time}})
    return {"ok": True}


@app.post("/rooms/{room_id}/start")
def start_room(room_id: str, start: StartIn):
    """
    Host presses Start once the room is full. This moves the room into the
    mandatory, uninterruptible national-anthem phase — no one (seated members
    or spectators) can speak until the anthem finishes and the frontend calls
    /rooms/{id}/anthem-complete.
    """
    room = rooms_col.find_one({"_id": oid(room_id)})
    if not room:
        raise HTTPException(404, "Room not found")
    if room["host_telegram_id"] != start.host_telegram_id:
        raise HTTPException(403, "Only the host can start this room")
    if room["status"] != "full":
        raise HTTPException(400, "Room must be full (8/8 seats) before starting")

    rooms_col.update_one(
        {"_id": oid(room_id)},
        {"$set": {"status": "anthem", "anthem_started_at": datetime.utcnow()}},
    )
    return {"ok": True, "status": "anthem"}


@app.post("/rooms/{room_id}/anthem-complete")
def anthem_complete(room_id: str):
    """Called by the frontend once the national anthem audio finishes playing."""
    result = rooms_col.update_one(
        {"_id": oid(room_id), "status": "anthem"}, {"$set": {"status": "live"}}
    )
    if result.matched_count == 0:
        raise HTTPException(400, "Room is not in the anthem phase")
    return {"ok": True, "status": "live"}


# ============ REVIEWS (post-debate feedback) ============

@app.post("/reviews")
def submit_review(review: ReviewIn):
    room = rooms_col.find_one({"_id": oid(review.room_id)})
    if not room:
        raise HTTPException(404, "Room not found")

    doc = review.model_dump()
    doc["created_at"] = datetime.utcnow()
    reviews_col.insert_one(doc)

    # Update the host's running rating average
    host_id = room["host_telegram_id"]
    host = users_col.find_one({"telegram_id": host_id})
    if host:
        old_count = host.get("host_rating_count", 0)
        old_avg = host.get("host_rating_avg", 0.0)
        new_count = old_count + 1
        new_avg = ((old_avg * old_count) + review.host_rating) / new_count
        users_col.update_one(
            {"telegram_id": host_id},
            {"$set": {"host_rating_avg": round(new_avg, 2), "host_rating_count": new_count}},
        )

    return {"ok": True}


@app.get("/rooms/{room_id}/reviews")
def get_room_reviews(room_id: str):
    """Lets you look back at what the 8 participants said after any past debate."""
    reviews = list(reviews_col.find({"room_id": room_id}))
    for r in reviews:
        r["id"] = str(r.pop("_id"))
    return reviews


@app.get("/")
def root():
    return {"status": "Debate Arena API is running"}


# ============ APP SETTINGS (background images etc.) ============

@app.get("/settings")
def get_settings():
    doc = settings_col.find_one({"_id": "app_settings"}) or {}
    return {
        "home_bg": doc.get("home_bg"),
        "messages_bg": doc.get("messages_bg"),
        "account_bg": doc.get("account_bg"),
    }


@app.post("/settings", dependencies=[Depends(require_admin)])
def update_settings(settings: AppSettings):
    settings_col.update_one(
        {"_id": "app_settings"},
        {"$set": {k: v for k, v in settings.model_dump().items() if v is not None}},
        upsert=True,
    )
    return {"ok": True}


# ============ MODERATION (admin only) ============

@app.get("/admin/rooms/active", dependencies=[Depends(require_admin)])
def list_active_rooms():
    """Every waiting/full/live room, for the admin panel to browse and join."""
    rooms = list(rooms_col.find({"status": {"$in": ["waiting", "full", "live"]}}))
    for r in rooms:
        r["id"] = str(r.pop("_id"))
        topic = topics_col.find_one({"_id": oid(r["topic_id"])}) if r.get("topic_id") else None
        r["topic_title"] = topic["title_en"] if topic else "(unknown topic)"
    return rooms


@app.post("/admin/rooms/{room_id}/warn-host", dependencies=[Depends(require_admin)])
def warn_host(room_id: str, warn: WarnIn):
    room = rooms_col.find_one({"_id": oid(room_id)})
    if not room:
        raise HTTPException(404, "Room not found")
    rooms_col.update_one(
        {"_id": oid(room_id)},
        {"$push": {"admin_warnings": {"message": warn.message, "at": datetime.utcnow()}}},
    )
    # NOTE: have your Telegram bot process poll/deliver this warning to the host,
    # or call the Telegram Bot API directly from here later.
    return {"ok": True}


@app.post("/admin/rooms/{room_id}/close", dependencies=[Depends(require_admin)])
def force_close_room(room_id: str):
    result = rooms_col.update_one(
        {"_id": oid(room_id)}, {"$set": {"status": "ended", "closed_by_admin": True}}
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Room not found")
    return {"ok": True}


@app.post("/admin/users/{telegram_id}/ban", dependencies=[Depends(require_admin)])
def ban_user(telegram_id: int, ban: BanIn):
    doc = {
        "telegram_id": telegram_id,
        "ban_type": ban.ban_type,
        "reason": ban.reason,
        "created_at": datetime.utcnow(),
        "until": (
            datetime.utcnow() + timedelta(hours=ban.hours)
            if ban.ban_type == "temporary" and ban.hours
            else None
        ),
    }
    bans_col.insert_one(doc)
    return {"ok": True}


@app.post("/admin/users/{telegram_id}/unban", dependencies=[Depends(require_admin)])
def unban_user(telegram_id: int):
    bans_col.delete_many({"telegram_id": telegram_id})
    return {"ok": True}
