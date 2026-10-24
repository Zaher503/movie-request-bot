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
