from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
from typing import Optional
from bson import ObjectId
import os

from database import (
    users_col, rooms_col, reviews_col, settings_col, bans_col,
    room_messages_col, host_applications_col, ensure_indexes
)
from models import (
    UserIn, RoomCreate, RoomJoin, ReviewIn,
    AppSettings, BanIn, WarnIn, SpectateIn, ChatMessageIn, ScheduleIn, StartIn,
    GuestApplyIn, GuestReviewIn, HostApplicationIn, HostReviewIn
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
                "is_host": False,  # must apply and be approved before creating rooms
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


@app.get("/users/{telegram_id}/host-status")
def get_host_status(telegram_id: int):
    user = users_col.find_one({"telegram_id": telegram_id})
    if user and user.get("is_host"):
        return {"status": "approved"}

    application = host_applications_col.find_one({"telegram_id": telegram_id})
    if not application:
        return {"status": "none"}

    if application["status"] == "pending":
        return {"status": "pending"}

    if application["status"] == "rejected":
        reviewed_at = application.get("reviewed_at")
        if reviewed_at and datetime.utcnow() - reviewed_at < timedelta(days=7):
            retry_at = reviewed_at + timedelta(days=7)
            return {"status": "rejected_waiting", "retry_at": retry_at.isoformat()}
        return {"status": "none"}  # cooldown is over — free to reapply

    return {"status": application["status"]}


# ============ HOST APPLICATIONS ============

@app.post("/host-applications")
def submit_host_application(application: HostApplicationIn):
    existing = host_applications_col.find_one({"telegram_id": application.telegram_id})
    if existing:
        if existing["status"] == "pending":
            raise HTTPException(400, "You already have a pending host application")
        if existing["status"] == "rejected":
            reviewed_at = existing.get("reviewed_at")
            if reviewed_at and datetime.utcnow() - reviewed_at < timedelta(days=7):
                raise HTTPException(403, "You must wait 7 days after a rejection before reapplying")

    doc = application.model_dump()
    doc["status"] = "pending"
    doc["created_at"] = datetime.utcnow()
    doc["reviewed_at"] = None
    host_applications_col.update_one(
        {"telegram_id": application.telegram_id}, {"$set": doc}, upsert=True
    )
    return {"ok": True}


@app.get("/admin/host-applications", dependencies=[Depends(require_admin)])
def list_host_applications(status: str = "pending"):
    apps = list(host_applications_col.find({"status": status}))
    for a in apps:
        a["id"] = str(a.pop("_id"))
    return apps


@app.post("/admin/host-applications/{telegram_id}/review", dependencies=[Depends(require_admin)])
def review_host_application(telegram_id: int, review: HostReviewIn):
    new_status = "approved" if review.approve else "rejected"
    result = host_applications_col.update_one(
        {"telegram_id": telegram_id},
        {"$set": {"status": new_status, "reviewed_at": datetime.utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Application not found")

    if review.approve:
        users_col.update_one({"telegram_id": telegram_id}, {"$set": {"is_host": True}})
    return {"ok": True, "status": new_status}


# ============ ROOMS ============

@app.get("/rooms/browse")
def browse_rooms(status: str = "waiting,full"):
    """
    Powers the Home screen's Active / Upcoming sections.
    Pass status=anthem,live for Active Rooms, or status=waiting,full for Upcoming Rooms.
    """
    statuses = [s.strip() for s in status.split(",")]
    rooms = list(rooms_col.find({"status": {"$in": statuses}}).sort("created_at", -1))
    for r in rooms:
        r["id"] = str(r.pop("_id"))
    return rooms


@app.post("/rooms")
def create_room(room: RoomCreate):
    host = users_col.find_one({"telegram_id": room.host_telegram_id})
    if not host or not host.get("is_host"):
        raise HTTPException(403, "You must be an approved host to create a room")

    doc = {
        "host_telegram_id": room.host_telegram_id,
        "topic_title": room.topic_title,
        "description": room.description,
        "questions": room.questions,  # the host's own questions, asked to anyone applying as a guest
        "pro_seats": [],
        "con_seats": [],
        "guests": [],  # [{telegram_id, name, answers, status: pending/approved/rejected}]
        "status": "waiting",
        "viewers": [],  # telegram_ids currently spectating (not seated, not a guest)
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


# ============ GUEST APPLICATIONS (audience seats beyond the 8 debaters) ============

@app.post("/rooms/{room_id}/apply-guest")
def apply_as_guest(room_id: str, application: GuestApplyIn):
    active_ban = bans_col.find_one({
        "telegram_id": application.telegram_id,
        "$or": [{"ban_type": "permanent"}, {"until": {"$gt": datetime.utcnow()}}],
    })
    if active_ban:
        raise HTTPException(403, "You are banned from applying to rooms.")

    room = rooms_col.find_one({"_id": oid(room_id)})
    if not room:
        raise HTTPException(404, "Room not found")
    if room["status"] not in ["waiting", "full"]:
        raise HTTPException(400, "This room isn't accepting guest applications anymore")
    if len(application.answers) != len(room["questions"]):
        raise HTTPException(400, f"Please answer all {len(room['questions'])} questions")
    if any(g["telegram_id"] == application.telegram_id for g in room.get("guests", [])):
        raise HTTPException(400, "You've already applied to this room")

    guest_doc = {
        "telegram_id": application.telegram_id,
        "name": application.name,
        "answers": application.answers,
        "status": "pending",
        "applied_at": datetime.utcnow(),
    }
    rooms_col.update_one({"_id": oid(room_id)}, {"$push": {"guests": guest_doc}})
    return {"ok": True}


@app.get("/rooms/{room_id}/guests")
def get_guests(room_id: str, host_telegram_id: int):
    room = rooms_col.find_one({"_id": oid(room_id)})
    if not room:
        raise HTTPException(404, "Room not found")
    if room["host_telegram_id"] != host_telegram_id:
        raise HTTPException(403, "Only the host can view guest applications")
    return room.get("guests", [])


@app.post("/rooms/{room_id}/guests/{telegram_id}/review")
def review_guest(room_id: str, telegram_id: int, review: GuestReviewIn):
    room = rooms_col.find_one({"_id": oid(room_id)})
    if not room:
        raise HTTPException(404, "Room not found")
    if room["host_telegram_id"] != review.host_telegram_id:
        raise HTTPException(403, "Only the host can approve or reject guests")

    new_status = "approved" if review.approve else "rejected"
    result = rooms_col.update_one(
        {"_id": oid(room_id), "guests.telegram_id": telegram_id},
        {"$set": {"guests.$.status": new_status}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Guest application not found")
    return {"ok": True, "status": new_status}


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
        "active_rooms_bg": doc.get("active_rooms_bg"),
        "upcoming_rooms_bg": doc.get("upcoming_rooms_bg"),
        "logo_image": doc.get("logo_image"),
        "anthem_audio_url": doc.get("anthem_audio_url"),
        "host_questions": doc.get("host_questions") or [
            "Why do you want to host debates on this app?",
            "How will you keep the debate respectful and on-topic?",
            "Describe a time you moderated or led a group discussion.",
        ],
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
    """Every waiting/full/anthem/live room, for the admin panel to browse and moderate."""
    rooms = list(rooms_col.find({"status": {"$in": ["waiting", "full", "anthem", "live"]}}))
    for r in rooms:
        r["id"] = str(r.pop("_id"))
    return rooms


@app.get("/admin/rooms/{room_id}/participants", dependencies=[Depends(require_admin)])
def get_room_participants(room_id: str):
    """
    Everyone tied to a room, for the admin to review and ban if needed:
    the host, the 8 seated debaters, approved guests, and anyone who's chatted.
    """
    room = rooms_col.find_one({"_id": oid(room_id)})
    if not room:
        raise HTTPException(404, "Room not found")

    def user_info(telegram_id):
        u = users_col.find_one({"telegram_id": telegram_id})
        return {"telegram_id": telegram_id, "name": u["name"] if u else "Unknown"}

    chatters = room_messages_col.find({"room_id": room_id})
    seen_chatters = {}
    for m in chatters:
        seen_chatters[m["telegram_id"]] = m["name"]

    return {
        "host": user_info(room["host_telegram_id"]),
        "pro_seats": [user_info(t) for t in room["pro_seats"]],
        "con_seats": [user_info(t) for t in room["con_seats"]],
        "guests": [g for g in room.get("guests", []) if g["status"] == "approved"],
        "chatters": [{"telegram_id": tid, "name": name} for tid, name in seen_chatters.items()],
    }


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
