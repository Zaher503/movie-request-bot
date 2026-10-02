import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request, send_from_directory

from grid import build_grid


def setting(name, default=None, required=False):
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


SEERR_URL = setting("SEERR_URL", "http://jellyseerr:5055").rstrip("/")
SEERR_API_KEY = setting("SEERR_API_KEY", required=True)
RADARR_URL = setting("RADARR_URL", "http://radarr:7878").rstrip("/")
RADARR_API_KEY = setting("RADARR_API_KEY", required=True)
SHARED_SECRET = setting("SHARED_SECRET", required=True)
PUBLIC_BASE = setting("PUBLIC_BASE", "https://zaher.site").rstrip("/")
DATA_DIR = Path(setting("DATA_DIR", "/data"))
TZ_NAME = setting("TZ", "America/Los_Angeles")
DAILY_LIMIT = int(setting("DAILY_LIMIT", "2"))
LIST_TTL = int(setting("LIST_TTL", "900"))
UNDO_TTL = int(setting("UNDO_TTL", "180"))
PAGE_SIZE = int(setting("PAGE_SIZE", "3"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMG_DIR = DATA_DIR / "img"
IMG_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
app = Flask(__name__)
message_lock = threading.Lock()
SENDER_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")
MOVIE_RE = re.compile(r"^movie:\s*(.*)$", re.IGNORECASE | re.DOTALL)
PICK_RE = re.compile(r"^(\d{1,2})$")


class MovieServerUnavailable(Exception):
    pass


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                sender TEXT PRIMARY KEY, query TEXT NOT NULL, results_json TEXT NOT NULL,
                next_page INTEGER NOT NULL, total_pages INTEGER NOT NULL,
                shown INTEGER NOT NULL, updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL,
                tmdb_id INTEGER NOT NULL, title TEXT NOT NULL, year TEXT NOT NULL,
                seerr_request_id INTEGER, seerr_media_id INTEGER,
                radarr_preexisting INTEGER NOT NULL, created_at REAL NOT NULL,
                undone INTEGER NOT NULL DEFAULT 0
            )
        """)


init_db()


def db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def seerr_headers():
    return {"X-Api-Key": SEERR_API_KEY}


def radarr_headers():
    return {"X-Api-Key": RADARR_API_KEY}


def seerr_search(query, page):
    try:
        # Seerr rejects reserved characters unless fully encoded
        response = requests.get(
            f"{SEERR_URL}/api/v1/search?query={quote(query, safe='')}&page={page}",
            headers=seerr_headers(), timeout=15,
        )
    except requests.RequestException as exc:
        raise MovieServerUnavailable() from exc
    if not response.ok:
        log.warning("Seerr search failed: %s %s", response.status_code, response.text[:300])
        raise MovieServerUnavailable()
    return response.json()


def seerr_get_movie(tmdb_id):
    try:
        response = requests.get(
            f"{SEERR_URL}/api/v1/movie/{tmdb_id}", headers=seerr_headers(), timeout=15,
        )
        if not response.ok:
            log.warning("Seerr movie lookup failed: %s %s", response.status_code, response.text[:300])
            return None
        return response.json()
    except (requests.RequestException, ValueError):
        log.warning("Seerr movie lookup failed for tmdb_id=%s", tmdb_id)
        return None


def movie_from_result(result):
    media_info = result.get("mediaInfo") or {}
    return {
        "tmdb_id": result.get("id"),
        "title": str(result.get("title") or "Unknown title"),
        "year": str(result.get("releaseDate") or "?")[:4] or "?",
        "rating": result.get("voteAverage") or 0,
        "vote_count": result.get("voteCount") or 0,
        "poster_path": result.get("posterPath"),
        "status": media_info.get("status"),
        "media_id": media_info.get("id"),
    }


def status_badge(status):
    if status == 5:
        return "✅ in library"
    if status == 4:
        return "✅ partly in library"
    if status in (2, 3):
        return "⏳ already requested"
    return None


def get_live_session(conn, sender, now):
    row = conn.execute("SELECT * FROM sessions WHERE sender = ?", (sender,)).fetchone()
    if row is None:
        return None
    if now - row["updated_at"] > LIST_TTL:
        conn.execute("DELETE FROM sessions WHERE sender = ?", (sender,))
        return None
    session = dict(row)
    session["results"] = json.loads(session.pop("results_json"))
    return session


def save_session(conn, sender, session):
    conn.execute("""
        INSERT INTO sessions (sender, query, results_json, next_page, total_pages, shown, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sender) DO UPDATE SET
          query=excluded.query, results_json=excluded.results_json,
          next_page=excluded.next_page, total_pages=excluded.total_pages,
          shown=excluded.shown, updated_at=excluded.updated_at
    """, (
        sender, session["query"], json.dumps(session["results"]), session["next_page"],
        session["total_pages"], session["shown"], session["updated_at"],
    ))


def fill_results(session, needed):
    fetches = 0
    while (len(session["results"]) < needed and session["next_page"] <= session["total_pages"]
           and fetches < 5):
        payload = seerr_search(session["query"], session["next_page"])
        page = session["next_page"]
        session["total_pages"] = int(payload.get("totalPages") or page)
        session["next_page"] = page + 1
        for result in payload.get("results") or []:
            if result.get("mediaType") == "movie" and result.get("id") is not None:
                session["results"].append(movie_from_result(result))
        fetches += 1


def clean_old_images():
    cutoff = time.time() - 3600
    for path in IMG_DIR.glob("*.jpg"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            log.warning("Could not remove old image: %s", path)


def list_reply(session, batch):
    start = session["shown"] + 1
    end = start + len(batch) - 1
    minutes = max(1, LIST_TTL // 60)
    lines = [f'🎬 {session["query"]} — reply !{start}–!{end}, "!more", or "!cancel" ({minutes} min)']
    for number, movie in enumerate(batch, start):
        line = f'!{number} {movie["title"]} ({movie["year"]})'
        if movie["vote_count"]:
            line += f' · ★{float(movie["rating"]):.1f}'
        badge = status_badge(movie.get("status"))
        if badge:
            line += f" · {badge}"
        lines.append(line)
    return "\n".join(lines)


def grid_for_batch(session, batch):
    clean_old_images()
    token = secrets.token_hex(16)
    items = []
    for number, movie in enumerate(batch, session["shown"] + 1):
        poster_path = movie.get("poster_path")
        items.append({
            "n": number,
            "poster_url": f"https://image.tmdb.org/t/p/w342{poster_path}" if poster_path else None,
            "title": movie["title"], "year": movie["year"],
            "badge": status_badge(movie.get("status")),
        })
    try:
        build_grid(items, str(IMG_DIR / f"{token}.jpg"))
        return f"{PUBLIC_BASE}/movie/img/{token}.jpg"
    except Exception:
        log.exception("Could not build result grid")
        return None


def show_next_batch(conn, sender, session, now):
    fill_results(session, session["shown"] + PAGE_SIZE)
    batch = session["results"][session["shown"]:session["shown"] + PAGE_SIZE]
    if not batch:
        session["updated_at"] = now
        save_session(conn, sender, session)
        return f'No more results for "{session["query"]}".', None
    reply = list_reply(session, batch)
    image_url = grid_for_batch(session, batch)
    session["shown"] += len(batch)
    session["updated_at"] = now
    save_session(conn, sender, session)
    return reply, image_url


def today_count(conn, sender, now):
    local = datetime.fromtimestamp(now, ZoneInfo(TZ_NAME))
    start = datetime(local.year, local.month, local.day, tzinfo=ZoneInfo(TZ_NAME)).timestamp()
    end = (datetime(local.year, local.month, local.day, tzinfo=ZoneInfo(TZ_NAME)) + timedelta(days=1)).timestamp()
    return conn.execute("""
        SELECT COUNT(*) FROM requests
        WHERE sender = ? AND undone = 0 AND created_at >= ? AND created_at < ?
    """, (sender, start, end)).fetchone()[0]


def radarr_preexisting(tmdb_id):
    try:
        response = requests.get(
            f"{RADARR_URL}/api/v3/movie", headers=radarr_headers(),
            params={"tmdbId": tmdb_id}, timeout=15,
        )
        if not response.ok:
            raise requests.RequestException(f"HTTP {response.status_code}")
        return bool(response.json())
    except (requests.RequestException, ValueError):
        log.warning("Radarr lookup failed for tmdb_id=%s; treating as pre-existing", tmdb_id)
        return True


def request_movie(conn, sender, number, now):
    session = get_live_session(conn, sender, now)
    if not session:
        return "That list expired. Send \"!movie: <title>\" to search again.", None
    if number < 1 or number > session["shown"]:
        return f'Pick a number from the list (!1–!{session["shown"]}).', None
    movie = session["results"][number - 1]
    details = seerr_get_movie(movie["tmdb_id"])
    if details is not None:
        media_info = details.get("mediaInfo") or {}
        status = media_info.get("status", movie.get("status"))
        media_id = media_info.get("id", movie.get("media_id"))
    else:
        status = movie.get("status")
        media_id = movie.get("media_id")
    if status in (4, 5):
        return f'✅ {movie["title"]} ({movie["year"]}) is already on Jellyfin.', None
    if status in (2, 3):
        return f'⏳ {movie["title"]} ({movie["year"]}) is already requested.', None
    used = today_count(conn, sender, now)
    if used >= DAILY_LIMIT:
        return f"Daily limit reached ({DAILY_LIMIT}/{DAILY_LIMIT}). Try again tomorrow.", None
    exists_in_radarr = radarr_preexisting(movie["tmdb_id"])
    try:
        response = requests.post(
            f"{SEERR_URL}/api/v1/request", headers=seerr_headers(),
            json={"mediaType": "movie", "mediaId": movie["tmdb_id"]}, timeout=15,
        )
    except requests.RequestException:
        log.warning("Seerr request failed for tmdb_id=%s", movie["tmdb_id"])
        return f'Something went wrong requesting {movie["title"]}. Try again in a bit.', None
    if response.status_code == 409:
        return f'⏳ {movie["title"]} ({movie["year"]}) is already requested.', None
    if not response.ok:
        log.warning("Seerr request failed: %s %s", response.status_code, response.text[:300])
        return f'Something went wrong requesting {movie["title"]}. Try again in a bit.', None
    payload = response.json()
    request_id = payload.get("id")
    media_id = (payload.get("media") or {}).get("id", media_id)
    conn.execute("""
        INSERT INTO requests (sender, tmdb_id, title, year, seerr_request_id, seerr_media_id,
                              radarr_preexisting, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (sender, movie["tmdb_id"], movie["title"], movie["year"], request_id, media_id,
          int(exists_in_radarr), now))
    remaining = DAILY_LIMIT - used - 1
    minutes = max(1, UNDO_TTL // 60)
    return (f'⬇️ Requested {movie["title"]} ({movie["year"]}). {remaining} of {DAILY_LIMIT} left today. '
            f'Reply "!undo" within {minutes} min to cancel.'), None


def cleanup_response(response, label, ignore_404=False):
    if response.ok or (ignore_404 and response.status_code == 404):
        return True
    log.warning("%s failed: %s %s", label, response.status_code, response.text[:300])
    return False


def delete_radarr_movie(tmdb_id):
    all_ok = True
    try:
        response = requests.get(
            f"{RADARR_URL}/api/v3/movie", headers=radarr_headers(),
            params={"tmdbId": tmdb_id}, timeout=15,
        )
        if not cleanup_response(response, "Radarr movie lookup"):
            return False
        movies = response.json()
    except (requests.RequestException, ValueError):
        log.exception("Radarr movie lookup failed during undo")
        return False
    for movie in movies:
        radarr_id = movie.get("id")
        if radarr_id is None:
            continue
        try:
            queue = requests.get(
                f"{RADARR_URL}/api/v3/queue", headers=radarr_headers(),
                params={"movieIds": radarr_id, "pageSize": 50}, timeout=15,
            )
            if cleanup_response(queue, "Radarr queue lookup"):
                for item in queue.json().get("records", []):
                    queue_id = item.get("id")
                    if queue_id is not None:
                        deleted = requests.delete(
                            f"{RADARR_URL}/api/v3/queue/{queue_id}", headers=radarr_headers(),
                            params={"removeFromClient": "true", "blocklist": "false"}, timeout=15,
                        )
                        all_ok = cleanup_response(deleted, "Radarr queue delete") and all_ok
            else:
                all_ok = False
            deleted = requests.delete(
                f"{RADARR_URL}/api/v3/movie/{radarr_id}", headers=radarr_headers(),
                params={"deleteFiles": "true", "addImportExclusion": "false"}, timeout=15,
            )
            all_ok = cleanup_response(deleted, "Radarr movie delete") and all_ok
        except (requests.RequestException, ValueError):
            log.exception("Radarr cleanup failed for movie_id=%s", radarr_id)
            all_ok = False
    return all_ok


def undo_request(conn, sender, now):
    row = conn.execute("""
        SELECT * FROM requests WHERE sender = ? AND undone = 0 AND created_at >= ?
        ORDER BY created_at DESC, id DESC LIMIT 1
    """, (sender, now - UNDO_TTL)).fetchone()
    if row is None:
        minutes = max(1, UNDO_TTL // 60)
        return f'Nothing to undo. "!undo" only works within {minutes} minutes of a pick.', None
    item = dict(row)
    all_ok = True
    try:
        if not item["radarr_preexisting"]:
            all_ok = delete_radarr_movie(item["tmdb_id"])
        if item["seerr_request_id"] is not None:
            try:
                response = requests.delete(
                    f"{SEERR_URL}/api/v1/request/{item['seerr_request_id']}",
                    headers=seerr_headers(), timeout=15,
                )
                all_ok = cleanup_response(response, "Seerr request delete", ignore_404=True) and all_ok
            except requests.RequestException:
                log.exception("Seerr request cleanup failed")
                all_ok = False
        if not item["radarr_preexisting"] and item["seerr_media_id"] is not None:
            try:
                response = requests.delete(
                    f"{SEERR_URL}/api/v1/media/{item['seerr_media_id']}",
                    headers=seerr_headers(), timeout=15,
                )
                all_ok = cleanup_response(response, "Seerr media delete", ignore_404=True) and all_ok
            except requests.RequestException:
                log.exception("Seerr media cleanup failed")
                all_ok = False
    except Exception:
        log.exception("Unexpected undo cleanup failure")
        all_ok = False
    conn.execute("UPDATE requests SET undone = 1 WHERE id = ?", (item["id"],))
    if all_ok:
        return f'↩️ Cancelled {item["title"]} ({item["year"]}). Download removed.', None
    return f'↩️ Cancelled {item["title"]} ({item["year"]}), but cleanup was incomplete. Check Radarr.', None


def handle_message(sender, text):
    now = time.time()
    message = text.strip()
    if not message.startswith("!"):
        log.info("message sender=%s command=ignored (no ! prefix)", sender)
        return "", None
    command_text = message[1:].strip()
    movie_match = MOVIE_RE.match(command_text)
    with db_connection() as conn:
        if movie_match:
            query = movie_match.group(1).strip()
            log.info("message sender=%s command=search", sender)
            if not query:
                return 'Send "!movie: <title>" to search, e.g. !movie: Scream', None
            session = {"query": query, "results": [], "next_page": 1, "total_pages": 1,
                       "shown": 0, "updated_at": now}
            try:
                fill_results(session, PAGE_SIZE)
            except MovieServerUnavailable:
                return "Couldn't reach the movie server. Try again later.", None
            conn.execute("DELETE FROM sessions WHERE sender = ?", (sender,))
            if not session["results"]:
                return f'Nothing found for "{query}". Check the spelling and try again.', None
            return show_next_batch(conn, sender, session, now)
        if PICK_RE.match(command_text):
            log.info("message sender=%s command=pick", sender)
            return request_movie(conn, sender, int(PICK_RE.match(command_text).group(1)), now)
        command = command_text.lower()
        if command == "more":
            log.info("message sender=%s command=more", sender)
            session = get_live_session(conn, sender, now)
            if not session:
                return 'No open list. Send "!movie: <title>" to search.', None
            try:
                return show_next_batch(conn, sender, session, now)
            except MovieServerUnavailable:
                return "Couldn't reach the movie server. Try again later.", None
        if command == "cancel":
            log.info("message sender=%s command=cancel", sender)
            conn.execute("DELETE FROM sessions WHERE sender = ?", (sender,))
            return 'Cancelled. Send "!movie: <title>" anytime.', None
        if command == "undo":
            log.info("message sender=%s command=undo", sender)
            return undo_request(conn, sender, now)
        log.info("message sender=%s command=ignored", sender)
        return "", None


@app.get("/movie/health")
def health():
    return "ok", 200


@app.get("/movie/img/<token>.jpg")
def image(token):
    if not TOKEN_RE.fullmatch(token):
        return "", 404
    return send_from_directory(IMG_DIR, f"{token}.jpg", mimetype="image/jpeg")


@app.route("/movie/message", methods=["POST"])
def message_endpoint():
    provided_key = request.headers.get("X-Movie-Key", "")
    if not hmac.compare_digest(provided_key, SHARED_SECRET):
        return jsonify(error="forbidden"), 403
    body = request.get_json(silent=True)
    # Shortcuts may send a form body instead of JSON
    if not isinstance(body, dict) and request.form:
        body = request.form.to_dict()
    if isinstance(body, dict) and isinstance(body.get("text"), (int, float)):
        body["text"] = str(body["text"])
    if not isinstance(body, dict) or not isinstance(body.get("sender"), str) or not isinstance(body.get("text"), str):
        shape = {k: type(v).__name__ for k, v in body.items()} if isinstance(body, dict) else type(body).__name__
        log.warning("Invalid request: content_type=%s body_shape=%s raw_len=%s",
                    request.content_type, shape, request.content_length)
        return jsonify(error="invalid request"), 400
    sender = body["sender"].strip().lower()
    if not SENDER_RE.fullmatch(sender):
        log.warning("Invalid sender format (length %s)", len(sender))
        return jsonify(error="invalid sender"), 400
    try:
        with message_lock:
            reply, image_url = handle_message(sender, body["text"])
        return jsonify(reply=reply, image_url=image_url)
    except Exception:
        log.error("Unexpected movie message error:\n%s", traceback.format_exc())
        return jsonify(reply="Movie bot hit an error. Try again later.", image_url=None)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
