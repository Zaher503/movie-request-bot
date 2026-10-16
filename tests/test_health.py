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
