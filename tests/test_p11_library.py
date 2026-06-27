"""P11 — media library scan + parsing + /api/library endpoint."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from subcast import library, webapp


# --------------------------------------------------------------------------- #
# parse_episode
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,season,episode", [
    ("The.Show.S01E02.1080p.mkv", 1, 2),
    ("the_show_s1e2.mkv", 1, 2),
    ("The Show 1x02.mkv", 1, 2),
    ("The Show 01x02 720p.mp4", 1, 2),
    ("The Show Season 1 Episode 2.mkv", 1, 2),
    ("The.Show.season.03.episode.10.mkv", 3, 10),
    ("Series.S10E125.mkv", 10, 125),
])
def test_parse_episode_positive(name, season, episode):
    got = library.parse_episode(name)
    assert got is not None
    assert got["season"] == season
    assert got["episode"] == episode
    assert got["series"]  # non-empty cleaned name


def test_parse_episode_series_name_cleaned():
    got = library.parse_episode("house.of.the.dragon.S01E03.2160p.HEVC.mkv")
    assert got["series"] == "House Of The Dragon"
    assert got["season"] == 1 and got["episode"] == 3


@pytest.mark.parametrize("name", [
    "Inception.2010.1080p.BluRay.x264.mkv",
    "random_movie.mp4",
    "vacation_2019.mov",
    "12.Angry.Men.mkv",
])
def test_parse_episode_negative(name):
    assert library.parse_episode(name) is None


def test_clean_title_strips_noise():
    assert library.clean_title("Inception.2010.1080p.BluRay.x264.mkv") \
        == "Inception 2010"
    assert library.clean_title("some_movie_name.mp4") == "Some Movie Name"
    # Trailing source/group tags after the year aren't enumerable release
    # tokens, but the year anchors the end of the title and they're dropped.
    assert library.clean_title(
        "Star-Wars-The-Mandalorian-and-Grogu-2026-1080p-DCPRiP-LiNE-x264-Robo29.mkv"
    ) == "Star Wars The Mandalorian And Grogu 2026"


# --------------------------------------------------------------------------- #
# scan_library
# --------------------------------------------------------------------------- #

def _make_tree(root):
    (root / "Movies").mkdir()
    (root / "Movies" / "Inception.2010.1080p.mkv").write_text("v")
    (root / "Movies" / "notes.txt").write_text("x")          # non-video
    (root / "Shows").mkdir()
    (root / "Shows" / "The.Show.S01E01.mkv").write_text("v")
    (root / "Shows" / "The.Show.S01E02.mkv").write_text("v")
    hidden = root / ".hidden"
    hidden.mkdir()
    (hidden / "secret.mkv").write_text("v")                  # must be skipped
    (root / ".sneaky.mkv").write_text("v")                   # hidden file, skip


def test_scan_library_basic(tmp_path):
    _make_tree(tmp_path)
    items = library.scan_library(str(tmp_path), refresh=True)
    paths = {i["name"] for i in items}
    assert paths == {
        "Inception.2010.1080p.mkv",
        "The.Show.S01E01.mkv",
        "The.Show.S01E02.mkv",
    }
    # Hidden dir/file excluded.
    assert not any("secret" in p or "sneaky" in p for p in paths)


def test_scan_library_fields(tmp_path):
    _make_tree(tmp_path)
    items = library.scan_library(str(tmp_path), refresh=True)
    byname = {i["name"]: i for i in items}
    ep = byname["The.Show.S01E02.mkv"]
    assert ep["series"] == "The Show"
    assert ep["season"] == 1 and ep["episode"] == 2
    assert ep["dir"] == "Shows"
    assert ep["size"] > 0
    assert isinstance(ep["mtime"], float)
    movie = byname["Inception.2010.1080p.mkv"]
    assert movie["series"] is None
    assert movie["title"] == "Inception 2010"
    # Each item carries a stable poster descriptor for the UI's art fetch.
    assert movie["art"]["key"] == "mv_inception_2010"
    assert movie["art"]["kind"] == "movie"
    assert ep["art"]["key"] == "tv_theshow" and ep["art"]["kind"] == "tv"


def test_scan_library_skips_dependency_dirs(tmp_path):
    # node_modules holds *.d.ts files that match the .ts (MPEG-TS) extension; they
    # must not be picked up as videos.
    (tmp_path / "Real.Movie.2020.mkv").write_text("v")
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.d.ts").write_text("x")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "bundle.ts").write_text("x")
    items = library.scan_library(str(tmp_path), refresh=True)
    assert {i["name"] for i in items} == {"Real.Movie.2020.mkv"}


def test_scan_library_invalid_root():
    assert library.scan_library("/no/such/dir/here", refresh=True) == []


def test_scan_library_cache(tmp_path):
    _make_tree(tmp_path)
    first = library.scan_library(str(tmp_path), refresh=True)
    # Add a file; without refresh the cached result is returned.
    (tmp_path / "Movies" / "New.Movie.2024.mkv").write_text("v")
    cached = library.scan_library(str(tmp_path))
    assert len(cached) == len(first)
    refreshed = library.scan_library(str(tmp_path), refresh=True)
    assert len(refreshed) == len(first) + 1


# --------------------------------------------------------------------------- #
# /api/library
# --------------------------------------------------------------------------- #

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


async def test_api_library(client, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    _make_tree(media)
    await client.post("/api/settings", json={"media_root": str(media)})

    r = await client.get("/api/library?refresh=1")
    assert r.status == 200
    body = await r.json()
    assert body["root"] == str(media)
    assert body["count"] == 3
    assert len(body["items"]) == 3
    assert {i["name"] for i in body["items"]} == {
        "Inception.2010.1080p.mkv",
        "The.Show.S01E01.mkv",
        "The.Show.S01E02.mkv",
    }


async def test_api_library_invalid_root(client, tmp_path):
    await client.post("/api/settings",
                      json={"media_root": str(tmp_path / "missing")})
    r = await client.get("/api/library")
    body = await r.json()
    assert r.status == 200
    assert body["items"] == []
    assert body["count"] == 0
    assert "error" in body
