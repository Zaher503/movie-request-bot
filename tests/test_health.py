import os
import tempfile

os.environ.update(
    SEERR_API_KEY="x",
    RADARR_API_KEY="x",
    SONARR_API_KEY="x",
    SHARED_SECRET="x",
    DATA_DIR=tempfile.mkdtemp(),
)

import app as bot  # noqa: E402


def test_healthz():
    assert bot.app.test_client().get("/healthz").get_json() == {"ok": True}


def test_seerr_request_missing_without_id():
    assert "SEERR-NOREQ" in bot.seerr_request_missing({"media": {"id": 108}}, "Show (2018)")


def test_seerr_request_missing_ok(monkeypatch):
    class Ok:
        ok = True

    monkeypatch.setattr(bot.requests, "get", lambda *a, **k: Ok())
    assert bot.seerr_request_missing({"id": 5}, "Show (2018)") is None


def test_resolve_tvdb_id_falls_back_to_imdb(monkeypatch):
    monkeypatch.setattr(bot, "sonarr_lookup_tvdb", lambda imdb: 345246 if imdb == "tt6763664" else None)
    assert bot.resolve_tvdb_id({"externalIds": {"tvdbId": None, "imdbId": "tt6763664"}}) == 345246
    assert bot.resolve_tvdb_id({"externalIds": {"tvdbId": 7}}) == 7
    assert bot.resolve_tvdb_id({"externalIds": {}}) is None


def test_sonarr_add_show_without_any_id(monkeypatch):
    monkeypatch.setattr(bot, "resolve_tvdb_id", lambda details: None)
    ok, reason = bot.sonarr_add_show({}, [1], "Show")
    assert not ok and "TVDB" in reason


def _show_setup(monkeypatch, have, total, seasons):
    posts = []

    class Ok:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(bot, "HA_WEBHOOK_URL", "http://ha/hook")
    monkeypatch.setattr(bot, "sonarr_series", lambda sid: {"tmdbId": 77, "seasons": [
        {"seasonNumber": 0, "statistics": {"episodeFileCount": 0, "episodeCount": 3}},
        {"seasonNumber": 1, "statistics": {"episodeFileCount": have, "episodeCount": total}},
    ]})
    monkeypatch.setattr(bot.requests, "post", lambda url, json=None, timeout=None: posts.append(json) or Ok())
    with bot.db_connection() as conn:
        conn.execute("DELETE FROM requests")
        conn.execute(
            "INSERT INTO requests (sender, tmdb_id, title, year, radarr_preexisting, created_at, media_type, seasons) "
            "VALUES ('me', 77, 'Show', '2020', 0, 0, 'tv', ?)", (bot.json.dumps(seasons),))
    return posts


def _sonarr_event(client):
    return client.post("/movie/downloaded", headers={"X-Movie-Key": "x"},
                       json={"eventType": "Download", "series": {"id": 5, "tmdbId": 77}})


def test_show_waits_for_all_requested_episodes(monkeypatch):
    posts = _show_setup(monkeypatch, 3, 8, [1])
    assert _sonarr_event(bot.app.test_client()).get_json()["notified"] == 0
    assert posts == []


def test_show_notifies_once_when_complete(monkeypatch):
    posts = _show_setup(monkeypatch, 8, 8, [1])
    client = bot.app.test_client()
    assert _sonarr_event(client).get_json()["notified"] == 1
    assert posts[0]["media_type"] == "tv" and posts[0]["detail"] == "season 1"
    assert _sonarr_event(client).get_json()["notified"] == 0
