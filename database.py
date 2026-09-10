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
rooms_col = db["rooms"]
reviews_col = db["reviews"]
settings_col = db["settings"]
bans_col = db["bans"]
room_messages_col = db["room_messages"]
host_applications_col = db["host_applications"]
direct_messages_col = db["direct_messages"]
levels_col = db["levels"]
background_shop_col = db["background_shop"]

# Helpful indexes (run once at startup — safe to call repeatedly)
def ensure_indexes():
    users_col.create_index("telegram_id", unique=True)
    rooms_col.create_index("status")
    reviews_col.create_index("room_id")
    bans_col.create_index("telegram_id")
    room_messages_col.create_index("room_id")
    host_applications_col.create_index("telegram_id", unique=True)
    direct_messages_col.create_index("conversation_id")
    levels_col.create_index("level", unique=True)
