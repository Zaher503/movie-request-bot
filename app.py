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
from concurrent.futures import ThreadPoolExecutor
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


SEERR_URL = setting("SEERR_URL", "http://seerr:5055").rstrip("/")
SEERR_API_KEY = setting("SEERR_API_KEY", required=True)
RADARR_URL = setting("RADARR_URL", "http://radarr:7878").rstrip("/")
RADARR_API_KEY = setting("RADARR_API_KEY", required=True)
SONARR_URL = setting("SONARR_URL", "http://sonarr:8989").rstrip("/")
SONARR_API_KEY = setting("SONARR_API_KEY")  # optional: lets &undo remove a show from Sonarr/qBit
SHARED_SECRET = setting("SHARED_SECRET", required=True)
PUBLIC_BASE = setting("PUBLIC_BASE", "https://zaher.site").rstrip("/")
DATA_DIR = Path(setting("DATA_DIR", "/data"))
TZ_NAME = setting("TZ", "America/Los_Angeles")
DAILY_LIMIT = int(setting("DAILY_LIMIT", "2"))
LIST_TTL = int(setting("LIST_TTL", "900"))
UNDO_TTL = int(setting("UNDO_TTL", "180"))
PAGE_SIZE = int(setting("PAGE_SIZE", "3"))
HA_WEBHOOK_URL = setting("HA_WEBHOOK_URL", "")

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
SHOW_RE = re.compile(r"^show:\s*(.*)$", re.IGNORECASE | re.DOTALL)
SEASON_RE = re.compile(r"^(?:season\s*|s)(\d{1,2})$", re.IGNORECASE)
PICK_RE = re.compile(r"^(\d{1,2})$")


@app.route("/healthz")
def healthz():
    return jsonify(ok=True)


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
        # Show support: columns added after the first release, so migrate in place.
        # For shows, radarr_preexisting means "Seerr already knew this show".
        for ddl in (
            "ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'movie'",
            "ALTER TABLE sessions ADD COLUMN pending_json TEXT",
            "ALTER TABLE requests ADD COLUMN media_type TEXT NOT NULL DEFAULT 'movie'",
            "ALTER TABLE requests ADD COLUMN seasons TEXT",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists
        columns = [row[1] for row in conn.execute("PRAGMA table_info(requests)")]
        if "notified" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN notified INTEGER NOT NULL DEFAULT 0")


init_db()


def db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def seerr_headers():
    return {"X-Api-Key": SEERR_API_KEY}


def radarr_headers():
    return {"X-Api-Key": RADARR_API_KEY}


def sonarr_headers():
    return {"X-Api-Key": SONARR_API_KEY}


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


def show_from_result(result):
    media_info = result.get("mediaInfo") or {}
    return {
        "tmdb_id": result.get("id"),
        "title": str(result.get("name") or "Unknown title"),
        "year": str(result.get("firstAirDate") or "?")[:4] or "?",
        "rating": result.get("voteAverage") or 0,
        "vote_count": result.get("voteCount") or 0,
        "poster_path": result.get("posterPath"),
        "status": media_info.get("status"),
        "media_id": media_info.get("id"),
    }


def seerr_get_tv(tmdb_id):
    try:
        response = requests.get(
            f"{SEERR_URL}/api/v1/tv/{tmdb_id}", headers=seerr_headers(), timeout=15,
        )
        if not response.ok:
            log.warning("Seerr tv lookup failed: %s %s", response.status_code, response.text[:300])
            return None
        return response.json()
    except (requests.RequestException, ValueError):
        log.warning("Seerr tv lookup failed for tmdb_id=%s", tmdb_id)
        return None


def season_info(details):
    """(all real season numbers, the ones not already in the library or requested)"""
    media_info = details.get("mediaInfo") or {}
    taken = {s.get("seasonNumber") for s in media_info.get("seasons") or []
             if s.get("status") in (2, 3, 4, 5)}
    numbers = sorted(s["seasonNumber"] for s in details.get("seasons") or []
                     if (s.get("seasonNumber") or 0) > 0)
    return numbers, [n for n in numbers if n not in taken]


def enrich_shows(batch):
    def load(show):
        details = seerr_get_tv(show["tmdb_id"])
        if details is None:
            return
        numbers, requestable = season_info(details)
        show["season_count"] = len(numbers) or int(details.get("numberOfSeasons") or 0)
        show["requestable"] = requestable
        show["status"] = (details.get("mediaInfo") or {}).get("status", show.get("status"))
    with ThreadPoolExecutor(max_workers=len(batch)) as pool:
        list(pool.map(load, batch))


def season_label(show):
    count = show.get("season_count")
    if count is None:
        return "? seasons"
    return f"{count} season{'' if count == 1 else 's'}"


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
    pending = session.pop("pending_json")
    session["pending"] = json.loads(pending) if pending else None
    return session


def save_session(conn, sender, session):
    conn.execute("""
        INSERT INTO sessions (sender, query, results_json, next_page, total_pages, shown, updated_at,
                              kind, pending_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sender) DO UPDATE SET
          query=excluded.query, results_json=excluded.results_json,
          next_page=excluded.next_page, total_pages=excluded.total_pages,
          shown=excluded.shown, updated_at=excluded.updated_at,
          kind=excluded.kind, pending_json=excluded.pending_json
    """, (
        sender, session["query"], json.dumps(session["results"]), session["next_page"],
        session["total_pages"], session["shown"], session["updated_at"],
        session["kind"], json.dumps(session["pending"]) if session.get("pending") else None,
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
            if result.get("id") is None:
                continue
            if session["kind"] == "show" and result.get("mediaType") == "tv":
                session["results"].append(show_from_result(result))
            elif session["kind"] == "movie" and result.get("mediaType") == "movie":
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
    lines = [f'{session["query"]}: reply &{start}-&{end}, &more or &cancel ({minutes} mins)']
    for number, movie in enumerate(batch, start):
        line = f'&{number} {movie["title"]} ({movie["year"]})'
        badge = status_badge(movie.get("status"))
        if badge:
            line += f" · {badge}"
        lines.append(line)
    return "\n".join(lines)


def show_list_reply(session, batch):
    start = session["shown"] + 1
    end = start + len(batch) - 1
    minutes = max(1, LIST_TTL // 60)
    lines = [f'{session["query"]}: reply &{start}-&{end}, &more or &cancel ({minutes} mins)']
    for number, show in enumerate(batch, start):
        line = f'&{number} {show["title"]} ({show["year"]}) · {season_label(show)}'
        badge = status_badge(show.get("status"))
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
            "subtitle": season_label(movie) if session["kind"] == "show" else None,
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
    if session["kind"] == "show":
        enrich_shows(batch)
        reply = show_list_reply(session, batch)
    else:
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
        return "That list expired. Send \"&movie: <title>\" to search again.", None
    if number < 1 or number > session["shown"]:
        return f'Pick a number from the list (&1–&{session["shown"]}).', None
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
            f'Reply "&undo" within {minutes} min to cancel.'), None


def pick_number(conn, sender, number, now):
    session = get_live_session(conn, sender, now)
    if session and session["kind"] == "show":
        return pick_show(conn, sender, session, number, now)
    return request_movie(conn, sender, number, now)


def pick_show(conn, sender, session, number, now):
    if number < 1 or number > session["shown"]:
        return f'Pick a number from the list (&1–&{session["shown"]}).', None
    show = session["results"][number - 1]
    label = f'{show["title"]} ({show["year"]})'
    details = seerr_get_tv(show["tmdb_id"])
    if details is None:
        return f"Couldn't load {label}. Try again in a bit.", None
    numbers, requestable = season_info(details)
    if not numbers:
        return f"{label} has no seasons listed yet.", None
    if not requestable:
        return f"✅ {label}: every season is already on Jellyfin or requested.", None
    media_info = details.get("mediaInfo") or {}
    session["pending"] = {
        "tmdb_id": show["tmdb_id"], "title": show["title"], "year": show["year"],
        "media_id": media_info.get("id"), "had_media": bool(media_info),
        "total": len(numbers), "requestable": requestable,
    }
    session["updated_at"] = now
    save_session(conn, sender, session)
    plural = "" if len(numbers) == 1 else "s"
    if len(requestable) == 1:
        return (f'{label} has {len(numbers)} season{plural}. Reply "&all" to download '
                f'season {requestable[0]}, or "&cancel".'), None
    reply = (f'{label} has {len(numbers)} season{plural}. Reply "&all" for all seasons, '
             f'or "&sx" for a specific season only.')
    if len(requestable) < len(numbers):
        reply += " Not yet on Jellyfin: " + ", ".join(str(n) for n in requestable) + "."
    return reply, None


def confirm_show(conn, sender, choice, now):
    session = get_live_session(conn, sender, now)
    pending = session and session.get("pending")
    if not pending:
        return 'Pick a show first. Send "&show: <title>" to search.', None
    label = f'{pending["title"]} ({pending["year"]})'
    requestable = pending["requestable"]
    if choice == "all":
        seasons = requestable
    elif choice in requestable:
        seasons = [choice]
    else:
        options = ", ".join(str(n) for n in requestable)
        return f"Season {choice} isn't available to request. Available: {options}.", None
    used = today_count(conn, sender, now)
    if used >= DAILY_LIMIT:
        return f"Daily limit reached ({DAILY_LIMIT}/{DAILY_LIMIT}). Try again tomorrow.", None
    try:
        response = requests.post(
            f"{SEERR_URL}/api/v1/request", headers=seerr_headers(),
            json={"mediaType": "tv", "mediaId": pending["tmdb_id"], "seasons": seasons}, timeout=15,
        )
    except requests.RequestException:
        log.warning("Seerr request failed for tmdb_id=%s", pending["tmdb_id"])
        return f'Something went wrong requesting {pending["title"]}. Try again in a bit.', None
    if response.status_code == 409:
        return f"⏳ {label} is already requested.", None
    if not response.ok:
        log.warning("Seerr request failed: %s %s", response.status_code, response.text[:300])
        return f'Something went wrong requesting {pending["title"]}. Try again in a bit.', None
    payload = response.json()
    media_id = (payload.get("media") or {}).get("id", pending.get("media_id"))
    conn.execute("""
        INSERT INTO requests (sender, tmdb_id, title, year, seerr_request_id, seerr_media_id,
                              radarr_preexisting, created_at, media_type, seasons)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'tv', ?)
    """, (sender, pending["tmdb_id"], pending["title"], pending["year"], payload.get("id"), media_id,
          int(pending["had_media"]), now, json.dumps(seasons)))
    conn.execute("DELETE FROM sessions WHERE sender = ?", (sender,))
    if len(seasons) == pending["total"]:
        what = f'all {pending["total"]} season{"" if pending["total"] == 1 else "s"}'
    elif len(seasons) == 1:
        what = f"season {seasons[0]}"
    else:
        what = "seasons " + ", ".join(str(n) for n in seasons)
    remaining = DAILY_LIMIT - used - 1
    minutes = max(1, UNDO_TTL // 60)
    return (f'⬇️ Requested {label}, {what}. {remaining} of {DAILY_LIMIT} left today. '
            f'Reply "&undo" within {minutes} min to cancel.'), None


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


def delete_sonarr_series(tmdb_id):
    if not SONARR_API_KEY:
        return False
    details = seerr_get_tv(tmdb_id) or {}
    tvdb_id = (details.get("externalIds") or {}).get("tvdbId") or (details.get("mediaInfo") or {}).get("tvdbId")
    if not tvdb_id:
        return False
    try:
        response = requests.get(
            f"{SONARR_URL}/api/v3/series", headers=sonarr_headers(),
            params={"tvdbId": tvdb_id}, timeout=15,
        )
        if not cleanup_response(response, "Sonarr series lookup"):
            return False
        series_list = response.json()
    except (requests.RequestException, ValueError):
        log.exception("Sonarr series lookup failed during undo")
        return False
    all_ok = True
    for series in series_list:
        series_id = series.get("id")
        if series_id is None:
            continue
        try:
            queue = requests.get(
                f"{SONARR_URL}/api/v3/queue", headers=sonarr_headers(),
                params={"pageSize": 200}, timeout=15,
            )
            if cleanup_response(queue, "Sonarr queue lookup"):
                for item in queue.json().get("records", []):
                    if item.get("seriesId") == series_id and item.get("id") is not None:
                        deleted = requests.delete(
                            f"{SONARR_URL}/api/v3/queue/{item['id']}", headers=sonarr_headers(),
                            params={"removeFromClient": "true", "blocklist": "false"}, timeout=15,
                        )
                        all_ok = cleanup_response(deleted, "Sonarr queue delete") and all_ok
            else:
                all_ok = False
            deleted = requests.delete(
                f"{SONARR_URL}/api/v3/series/{series_id}", headers=sonarr_headers(),
                params={"deleteFiles": "true", "addImportExclusion": "false"}, timeout=15,
            )
            all_ok = cleanup_response(deleted, "Sonarr series delete") and all_ok
        except (requests.RequestException, ValueError):
            log.exception("Sonarr cleanup failed for series_id=%s", series_id)
            all_ok = False
    return all_ok


def undo_show(conn, item):
    label = f'{item["title"]} ({item["year"]})'
    all_ok = True
    try:
        # Only touch Sonarr/Seerr media when this request is what added the show.
        if not item["radarr_preexisting"]:
            all_ok = delete_sonarr_series(item["tmdb_id"])
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
        log.exception("Unexpected show undo cleanup failure")
        all_ok = False
    conn.execute("UPDATE requests SET undone = 1 WHERE id = ?", (item["id"],))
    if all_ok:
        return f"↩️ Cancelled {label}. Download removed.", None
    return f"↩️ Cancelled {label}, but cleanup was incomplete. Check Sonarr.", None


def undo_request(conn, sender, now):
    row = conn.execute("""
        SELECT * FROM requests WHERE sender = ? AND undone = 0 AND created_at >= ?
        ORDER BY created_at DESC, id DESC LIMIT 1
    """, (sender, now - UNDO_TTL)).fetchone()
    if row is None:
        minutes = max(1, UNDO_TTL // 60)
        return f'Nothing to undo. "&undo" only works within {minutes} minutes of a pick.', None
    item = dict(row)
    if item["media_type"] == "tv":
        return undo_show(conn, item)
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


def format_minutes(seconds):
    minutes = max(1, -(-int(seconds) // 60))
    if minutes < 120:
        return f"~{minutes} min"
    return f"~{minutes // 60} h {minutes % 60} min"


def parse_timeleft(value):
    """Radarr timeleft looks like 'hh:mm:ss' or 'd.hh:mm:ss'."""
    if not value:
        return None
    try:
        days = 0
        if "." in value.split(":")[0]:
            day_part, value = value.split(".", 1)
            days = int(day_part)
        hours, minutes, seconds = (int(float(part)) for part in value.split(":"))
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    except ValueError:
        return None


def movie_eta_line(item):
    """One line about a requested movie: minutes left, or why there is no number."""
    label = f'{item["title"]} ({item["year"]})'
    try:
        response = requests.get(
            f"{RADARR_URL}/api/v3/movie", headers=radarr_headers(),
            params={"tmdbId": item["tmdb_id"]}, timeout=15,
        )
        response.raise_for_status()
        movies = response.json()
        if not movies:
            return f"{label}: not in the download list yet, check back in a minute."
        movie = movies[0]
        if movie.get("hasFile"):
            return f"{label}: ✅ already downloaded."
        queue = requests.get(
            f"{RADARR_URL}/api/v3/queue", headers=radarr_headers(),
            params={"movieIds": movie["id"], "pageSize": 50}, timeout=15,
        )
        queue.raise_for_status()
        records = queue.json().get("records", [])
    except (requests.RequestException, ValueError, KeyError):
        log.warning("ETA lookup failed for tmdb_id=%s", item["tmdb_id"])
        return f"{label}: couldn't reach the download server."
    if not records:
        return f"{label}: still searching for a release, no download yet."
    record = records[0]
    left = parse_timeleft(record.get("timeleft"))
    size = float(record.get("size") or 0)
    done = 100 - round(100 * float(record.get("sizeleft") or 0) / size) if size else 0
    state = record.get("trackedDownloadState") or ""
    if state == "importPending" or state == "importing":
        return f"{label}: downloaded, being added to Jellyfin now."
    if left is None or left <= 0 or record.get("status") in ("stalled", "queued", "paused"):
        return f"{label}: {done}% done, no estimate yet (waiting for speed)."
    return f"{label}: {format_minutes(left)} left ({done}% done)."


def seasons_label(seasons):
    if len(seasons) == 1:
        return f"season {seasons[0]}"
    return "seasons " + ", ".join(str(n) for n in seasons)


def show_eta_line(item):
    """One line about a requested show, from Sonarr: minutes left, or why there is no number."""
    wanted = json.loads(item["seasons"]) if item.get("seasons") else []
    label = f'{item["title"]} ({item["year"]})'
    if wanted:
        label += f", {seasons_label(wanted)}"
    if not SONARR_API_KEY:
        return f"{label}: no estimate available (Sonarr isn't connected)."
    details = seerr_get_tv(item["tmdb_id"]) or {}
    tvdb_id = (details.get("externalIds") or {}).get("tvdbId") or (details.get("mediaInfo") or {}).get("tvdbId")
    if not tvdb_id:
        return f"{label}: couldn't look this one up right now."
    try:
        response = requests.get(
            f"{SONARR_URL}/api/v3/series", headers=sonarr_headers(),
            params={"tvdbId": tvdb_id}, timeout=15,
        )
        response.raise_for_status()
        series_list = response.json()
        if not series_list:
            return f"{label}: not in the download list yet, check back in a minute."
        series = series_list[0]
        queue = requests.get(
            f"{SONARR_URL}/api/v3/queue", headers=sonarr_headers(),
            params={"pageSize": 200, "includeEpisode": "true"}, timeout=15,
        )
        queue.raise_for_status()
        records = queue.json().get("records", [])
    except (requests.RequestException, ValueError, KeyError):
        log.warning("Show ETA lookup failed for tmdb_id=%s", item["tmdb_id"])
        return f"{label}: couldn't reach the download server."
    have = total = 0
    for season in series.get("seasons") or []:
        if not wanted or season.get("seasonNumber") in wanted:
            stats = season.get("statistics") or {}
            have += int(stats.get("episodeFileCount") or 0)
            total += int(stats.get("episodeCount") or 0)
    mine = []
    for record in records:
        if record.get("seriesId") != series.get("id"):
            continue
        number = record.get("seasonNumber")
        if number is None:
            number = (record.get("episode") or {}).get("seasonNumber")
        if not wanted or number is None or number in wanted:
            mine.append(record)
    if not mine:
        if total and have >= total:
            return f"{label}: ✅ already downloaded."
        if have:
            return f"{label}: {have} of {total} episodes in, still searching for the rest."
        return f"{label}: still searching for a release, no download yet."
    # A season pack is one download shared by many episode rows: count each download once.
    downloads = {}
    for record in mine:
        downloads.setdefault(record.get("downloadId") or record.get("id"), record)
    size = sum(float(r.get("size") or 0) for r in downloads.values())
    size_left = sum(float(r.get("sizeleft") or 0) for r in downloads.values())
    done = 100 - round(100 * size_left / size) if size else 0
    states = {r.get("trackedDownloadState") or "" for r in downloads.values()}
    if states <= {"importPending", "importing"}:
        return f"{label}: downloaded, being added to Jellyfin now."
    lefts = [parse_timeleft(r.get("timeleft")) for r in downloads.values()]
    stuck = any(r.get("status") in ("stalled", "queued", "paused") for r in downloads.values())
    if None in lefts or stuck or max(lefts) <= 0:
        return f"{label}: {done}% done, no estimate yet (waiting for speed)."
    return f"{label}: {format_minutes(max(lefts))} left ({done}% done)."


def request_eta_line(item):
    return show_eta_line(item) if item.get("media_type") == "tv" else movie_eta_line(item)


def eta_reply(conn, sender, argument):
    rows = conn.execute("""
        SELECT * FROM requests WHERE sender = ? AND undone = 0
        ORDER BY created_at DESC, id DESC LIMIT 5
    """, (sender,)).fetchall()
    if not rows:
        return "No requests yet. Send \"&movie: <title>\" to ask for one.", None
    items = [dict(row) for row in rows]
    if argument == "all":
        lines = [request_eta_line(item) for item in items]
        return "⏱️ Your last requests:\n" + "\n".join(lines), None
    if argument.isdigit() and 1 <= int(argument) <= len(items):
        return "⏱️ " + request_eta_line(items[int(argument) - 1]), None
    reply = "⏱️ " + request_eta_line(items[0])
    if len(items) > 1:
        reply += '\nMore in the list: "&ETA all", or "&ETA 2" for the one before.'
    return reply, None


def handle_message(sender, text):
    now = time.time()
    message = text.strip()
    if not message.startswith("&"):
        log.info("message sender=%s command=ignored (no & prefix)", sender)
        return "", None
    command_text = message[1:].strip()
    movie_match = MOVIE_RE.match(command_text)
    show_match = SHOW_RE.match(command_text)
    with db_connection() as conn:
        if movie_match or show_match:
            kind = "movie" if movie_match else "show"
            query = (movie_match or show_match).group(1).strip()
            log.info("message sender=%s command=search kind=%s", sender, kind)
            if not query:
                if kind == "show":
                    return 'Send "&show: <title>" to search, e.g. &show: Severance', None
                return 'Send "&movie: <title>" to search, e.g. &movie: Scream', None
            session = {"query": query, "results": [], "next_page": 1, "total_pages": 1,
                       "shown": 0, "updated_at": now, "kind": kind, "pending": None}
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
            return pick_number(conn, sender, int(PICK_RE.match(command_text).group(1)), now)
        season_match = SEASON_RE.match(command_text)
        if season_match:
            log.info("message sender=%s command=season", sender)
            return confirm_show(conn, sender, int(season_match.group(1)), now)
        command = command_text.lower()
        if command == "all":
            log.info("message sender=%s command=all", sender)
            return confirm_show(conn, sender, "all", now)
        if command == "more":
            log.info("message sender=%s command=more", sender)
            session = get_live_session(conn, sender, now)
            if not session:
                return 'No open list. Send "&movie: <title>" to search.', None
            try:
                return show_next_batch(conn, sender, session, now)
            except MovieServerUnavailable:
                return "Couldn't reach the movie server. Try again later.", None
        if command == "cancel":
            log.info("message sender=%s command=cancel", sender)
            conn.execute("DELETE FROM sessions WHERE sender = ?", (sender,))
            return ('Cancelled. Send "&movie: <title>" or "&show: <title>" anytime, '
                    'or "&eta" to check on your requests.'), None
        if command == "eta" or command.startswith("eta "):
            log.info("message sender=%s command=eta", sender)
            return eta_reply(conn, sender, command[3:].strip())
        if command == "undo":
            log.info("message sender=%s command=undo", sender)
            return undo_request(conn, sender, now)
        log.info("message sender=%s command=ignored", sender)
        return "", None


@app.post("/movie/downloaded")
def downloaded_endpoint():
    """Radarr 'On Import' webhook: tell Home Assistant to notify whoever requested the movie."""
    provided_key = request.headers.get("X-Movie-Key", "")
    if not hmac.compare_digest(provided_key, SHARED_SECRET):
        return jsonify(error="forbidden"), 403
    body = request.get_json(silent=True) or {}
    if body.get("eventType") != "Download":
        return jsonify(ok=True, skipped=body.get("eventType")), 200
    tmdb_id = (body.get("movie") or {}).get("tmdbId")
    if not tmdb_id or not HA_WEBHOOK_URL:
        return jsonify(ok=True, skipped="no tmdbId or HA_WEBHOOK_URL"), 200
    with message_lock, db_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM requests WHERE tmdb_id = ? AND media_type = 'movie' AND undone = 0 AND notified = 0
        """, (tmdb_id,)).fetchall()
        for row in rows:
            try:
                response = requests.post(HA_WEBHOOK_URL, json={
                    "sender": row["sender"], "title": row["title"],
                    "year": row["year"], "tmdb_id": tmdb_id,
                }, timeout=10)
                response.raise_for_status()
                conn.execute("UPDATE requests SET notified = 1 WHERE id = ?", (row["id"],))
                log.info("notified sender=%s tmdb_id=%s", row["sender"], tmdb_id)
            except requests.RequestException:
                log.exception("Home Assistant notify failed for tmdb_id=%s", tmdb_id)
    return jsonify(ok=True, notified=len(rows)), 200


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
