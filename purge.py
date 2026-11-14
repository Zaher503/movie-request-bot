"""Remove a movie or show everywhere: qBittorrent, Radarr/Sonarr, Seerr and Jellyfin."""
import logging
import os
import re

import requests

log = logging.getLogger("purge")

RADARR_URL = os.getenv("RADARR_URL", "http://radarr:7878").rstrip("/")
SONARR_URL = os.getenv("SONARR_URL", "http://sonarr:8989").rstrip("/")
SEERR_URL = os.getenv("SEERR_URL", "http://seerr:5055").rstrip("/")
QBIT_URL = os.getenv("QBIT_URL", "http://qbittorrent:8080").rstrip("/")
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "http://jellyfin:8096").rstrip("/")


def _key(name):
    return {"X-Api-Key": os.getenv(name, "")}


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def find_library(query, allow_all=False):
    """Return library items whose title contains the query. An empty query lists everything if allow_all."""
    wanted = _norm(query)
    if not wanted and not allow_all:
        return []
    items = []
    for kind, base, key, path in (
        ("movie", RADARR_URL, "RADARR_API_KEY", "/api/v3/movie"),
        ("show", SONARR_URL, "SONARR_API_KEY", "/api/v3/series"),
    ):
        try:
            response = requests.get(base + path, headers=_key(key), timeout=20)
            response.raise_for_status()
        except requests.RequestException:
            log.exception("library lookup failed for %s", kind)
            continue
        for entry in response.json():
            if wanted in _norm(entry.get("title", "")):
                stats = entry.get("statistics") or {}
                size = (entry.get("movieFile") or {}).get("size") or stats.get("sizeOnDisk") or 0
                items.append({
                    "kind": kind, "id": entry["id"], "title": entry["title"],
                    "year": entry.get("year"), "tmdb_id": entry.get("tmdbId"), "size": size,
                })
    items.sort(key=lambda i: (_norm(i["title"]) != wanted, i["title"].lower()))
    return items


def _qbit_session():
    session = requests.Session()
    response = session.post(
        QBIT_URL + "/api/v2/auth/login",
        data={"username": os.getenv("QBIT_USER", ""), "password": os.getenv("QBIT_PASSWORD", "")},
        headers={"Referer": QBIT_URL}, timeout=15,
    )
    if response.text.strip() != "Ok.":
        raise RuntimeError("qBittorrent login failed")
    session.headers["Referer"] = QBIT_URL
    return session


def _history_hashes(kind, item_id):
    if kind == "movie":
        url, headers, params = RADARR_URL + "/api/v3/history/movie", _key("RADARR_API_KEY"), {"movieId": item_id}
    else:
        url, headers, params = SONARR_URL + "/api/v3/history/series", _key("SONARR_API_KEY"), {"seriesId": item_id}
    response = requests.get(url, headers=headers, params=params, timeout=20)
    response.raise_for_status()
    return {r["downloadId"].lower() for r in response.json() if r.get("downloadId")}


def _queue_ids(kind, item_id):
    base, key = (RADARR_URL, "RADARR_API_KEY") if kind == "movie" else (SONARR_URL, "SONARR_API_KEY")
    response = requests.get(base + "/api/v3/queue", headers=_key(key), params={"pageSize": 500}, timeout=20)
    response.raise_for_status()
    field = "movieId" if kind == "movie" else "seriesId"
    return [r["id"] for r in response.json().get("records", []) if r.get(field) == item_id]


def purge(kind, item_id):
    """Remove one library item everywhere. Returns a list of (step, ok, detail)."""
    steps = []
    base, key = (RADARR_URL, "RADARR_API_KEY") if kind == "movie" else (SONARR_URL, "SONARR_API_KEY")
    path = "/api/v3/movie/" if kind == "movie" else "/api/v3/series/"
    try:
        info = requests.get(base + path + str(item_id), headers=_key(key), timeout=20)
        info.raise_for_status()
        info = info.json()
    except requests.RequestException as exc:
        return [("lookup", False, str(exc))]

    # Torrents first, so their files go too (the library file is a hardlink of the same data)
    try:
        hashes = _history_hashes(kind, item_id)
        for queue_id in _queue_ids(kind, item_id):
            requests.delete(f"{base}/api/v3/queue/{queue_id}", headers=_key(key),
                            params={"removeFromClient": "true", "blocklist": "false"}, timeout=20)
        if hashes:
            session = _qbit_session()
            present = {t["hash"] for t in session.get(QBIT_URL + "/api/v2/torrents/info", timeout=20).json()}
            doomed = sorted(hashes & present)
            if doomed:
                session.post(QBIT_URL + "/api/v2/torrents/delete",
                             data={"hashes": "|".join(doomed), "deleteFiles": "true"}, timeout=30).raise_for_status()
            steps.append(("qBittorrent", True, f"{len(doomed)} torrent(s) removed"))
        else:
            steps.append(("qBittorrent", True, "no torrents in history"))
    except (requests.RequestException, RuntimeError) as exc:
        log.exception("qBittorrent step failed")
        steps.append(("qBittorrent", False, str(exc)))

    # Radarr/Sonarr: delete the item and its library files
    try:
        response = requests.delete(base + path + str(item_id), headers=_key(key),
                                   params={"deleteFiles": "true", "addImportExclusion": "false"}, timeout=60)
        response.raise_for_status()
        steps.append(("Radarr" if kind == "movie" else "Sonarr", True, "deleted with files"))
    except requests.RequestException as exc:
        steps.append(("Radarr" if kind == "movie" else "Sonarr", False, str(exc)))

    # Seerr: clear the media entry so it can be requested again
    try:
        tmdb_id = info.get("tmdbId")
        route = "movie" if kind == "movie" else "tv"
        media = requests.get(f"{SEERR_URL}/api/v1/{route}/{tmdb_id}", headers=_key("SEERR_API_KEY"), timeout=20)
        media_id = (media.json().get("mediaInfo") or {}).get("id") if media.ok else None
        if media_id:
            requests.delete(f"{SEERR_URL}/api/v1/media/{media_id}", headers=_key("SEERR_API_KEY"),
                            timeout=20).raise_for_status()
            steps.append(("Seerr", True, "reset, can be requested again"))
        else:
            steps.append(("Seerr", True, "not tracked"))
    except (requests.RequestException, ValueError) as exc:
        steps.append(("Seerr", False, str(exc)))

    # Jellyfin: rescan so the title disappears
    try:
        response = requests.post(
            JELLYFIN_URL + "/Library/Refresh",
            headers={"Authorization": f'MediaBrowser Token="{os.getenv("JELLYFIN_API_KEY", "")}"'}, timeout=20)
        response.raise_for_status()
        steps.append(("Jellyfin", True, "library refresh started"))
    except requests.RequestException as exc:
        steps.append(("Jellyfin", False, str(exc)))
    return steps
