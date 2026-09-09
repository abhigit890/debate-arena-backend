# Debate Arena — Backend

## Files
- `main.py` — FastAPI app with all endpoints (users, topics, rooms, reviews)
- `models.py` — data shapes for each request
- `database.py` — MongoDB connection
- `requirements.txt` — Python packages needed
- `.env.example` — shows which environment variables to set (copy to `.env` locally, never commit `.env`)

## Endpoints at a glance
- `POST /users` — create/update a user (telegram_id, name, state, district, language)
- `GET /users/{telegram_id}` — get a user's profile + host stats
- `POST /topics?is_admin=true` — add a topic (admin, auto-approved) or `is_admin=false` (user suggestion, goes to "pending")
- `GET /topics?state=X&district=Y` — list approved topics for a state (district-matching ones sorted first)
- `POST /topics/{id}/review` — admin approves/rejects a suggested topic
- `POST /rooms` — host schedules a new debate room for a topic
- `POST /rooms/{id}/join` — a user joins a room on the "pro" or "con" side (max 4 each)
- `GET /rooms/{id}` — room details incl. current seats
- `POST /rooms/{id}/end` — host ends the debate; increments their hosted-debate count
- `POST /reviews` — a participant submits host rating (1-5) + their final opinion after a debate
- `GET /rooms/{id}/reviews` — see all past reviews for a specific debate

## Deploying to Render (free)

1. Push this folder to a **new GitHub repository** (e.g. `debate-arena-backend`).
2. Go to render.com → sign up with GitHub (free).
3. "New +" → "Web Service" → pick your `debate-arena-backend` repo.
4. Settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Under "Environment Variables", add:
   - `MONGO_URI` = your MongoDB connection string (with the real password in it)
   - `DB_NAME` = `debate_arena`
6. Click "Create Web Service". After a couple of minutes you'll get a live URL like:
   `https://debate-arena-backend.onrender.com`
7. Visit that URL — you should see `{"status": "Debate Arena API is running"}`

**Note:** Render's free tier "sleeps" after 15 minutes of no traffic, so the first
request after a while takes a few extra seconds to wake up. Fine for now; something
to revisit once there's real usage.

## Next step
Once this is live, the Mini App (the state/district screen we built) will call
these endpoints — e.g. `POST /users` right when someone opens the app, and
`GET /topics` to show them relevant debates.
