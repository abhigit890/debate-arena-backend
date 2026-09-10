import os
import certifi
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "debate_arena")

if not MONGO_URI:
    raise RuntimeError(
        "MONGO_URI is not set. Add it to your .env file locally, "
        "or as an Environment Variable on Render."
    )

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[DB_NAME]

# Collections — one per "table"
users_col = db["users"]
topics_col = db["topics"]
rooms_col = db["rooms"]
reviews_col = db["reviews"]

# Helpful indexes (run once at startup — safe to call repeatedly)
def ensure_indexes():
    users_col.create_index("telegram_id", unique=True)
    topics_col.create_index([("state", 1), ("district", 1)])
    rooms_col.create_index("topic_id")
    rooms_col.create_index("status")
    reviews_col.create_index("room_id")
