"""P10 — webapp settings + subtitle-search endpoints (network mocked)."""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from subcast import subsearch, webapp


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


@pytest.fixture
async def client(cfg_home):
    app = webapp.build_app()
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


async def test_settings_get_then_post_roundtrip(client):
    r = await client.get("/api/settings")
    body = await r.json()
    assert r.status == 200
    assert "system_language" in body
    assert body["has_api_key"] is False

    r = await client.post("/api/settings", json={
        "media_root": "/movies", "preferred_sub_lang": "cs",
        "opensubtitles_api_key": "secret",
    })
    saved = await r.json()
    assert saved["media_root"] == "/movies"
    assert saved["preferred_sub_lang"] == "cs"
    assert saved["has_api_key"] is True

    # Persisted across a fresh GET.
    r = await client.get("/api/settings")
    body = await r.json()
    assert body["media_root"] == "/movies"


async def test_subsearch_local_sidecar(client, cfg_home, tmp_path):
    video = tmp_path / "Movie.mkv"
    video.write_text("v")
    (tmp_path / "Movie.srt").write_text("s")
    # media_root default is cwd; point it at tmp_path explicitly.
    await client.post("/api/settings", json={"media_root": str(tmp_path)})

    r = await client.post("/api/subsearch", json={"source": str(video)})
    body = await r.json()
    assert body["sidecar"].endswith("Movie.srt")
    assert body["online_available"] is False


async def test_subsearch_remote_source(client):
    r = await client.post("/api/subsearch",
                          json={"source": "http://example.com/x.mp4"})
    body = await r.json()
    assert body["sidecar"] is None
    assert body["local"] == []


async def test_subsearch_online_no_key_errors(client, tmp_path):
    video = tmp_path / "Movie.mkv"
    video.write_text("v")
    r = await client.post("/api/subsearch/online", json={"source": str(video)})
    assert r.status == 400
    body = await r.json()
    assert "error" in body


async def test_subdownload_flow(client, monkeypatch, tmp_path):
    await client.post("/api/settings", json={"opensubtitles_api_key": "KEY"})

    def fake_urlopen(req, timeout=None):
        class _R:
            def __init__(self, data):
                self._d = data
            def read(self):
                return self._d
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        if req.full_url.endswith("/download"):
            return _R(json.dumps({"link": "https://cdn/x.srt",
                                  "file_name": "Movie.srt"}).encode())
        return _R(b"WEBVTT\n")

    monkeypatch.setattr(subsearch.urllib.request, "urlopen", fake_urlopen)
    r = await client.post("/api/subdownload", json={"file_id": "9"})
    body = await r.json()
    assert r.status == 200
    assert body["path"].endswith("Movie.srt")
