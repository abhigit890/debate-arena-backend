from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from bson import ObjectId

from database import users_col, topics_col, rooms_col, reviews_col, ensure_indexes
from models import (
    UserIn, TopicIn, TopicApprove, RoomCreate, RoomJoin, ReviewIn
)

app = FastAPI(title="Debate Arena API")

# Allow requests from your Telegram Mini App (hosted on Vercel)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this to your Vercel URL once live
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        "created_at": datetime.utcnow(),
    }
    result = rooms_col.insert_one(doc)
    return {"id": str(result.inserted_id)}


@app.post("/rooms/{room_id}/join")
def join_room(room_id: str, join: RoomJoin):
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
