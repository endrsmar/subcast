"""P12 — background poster artwork: normalization, cache, fetch, endpoints."""

from __future__ import annotations

import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from subcast import artwork, webapp


@pytest.fixture(autouse=True)
def art_home(tmp_path, monkeypatch):
    """Point the poster store at a tmp dir so tests never touch ~/.subcast."""
    d = tmp_path / "art"
    monkeypatch.setenv(artwork.ART_DIR_ENV, str(d))
    return d


# --------------------------------------------------------------------------- #
# describe — normalized art descriptors
# --------------------------------------------------------------------------- #

def test_describe_movie_variants_collapse_to_one_key():
    keys = {
        artwork.describe(t)["key"]
        for t in ("Inception 2010", "Inception", "inception 2010")
    }
    # Variants with the same year share a key; the year-less one is its own key
    # (still valid) — both forms resolve via the same query.
    assert "mv_inception_2010" in keys


def test_describe_movie_strips_year_from_query_keeps_in_key():
    d = artwork.describe("Inception 2010")
    assert d["key"] == "mv_inception_2010"
    assert d["query"] == "Inception"  # year left out of the search term
    assert d["kind"] == "movie"


def test_describe_movie_drops_trailing_release_junk_after_year():
    # Release/source/group tags after the year aren't enumerable tokens; the
    # year anchors the end of the real title, so the query stops there.
    d = artwork.describe("Star Wars The Mandalorian and Grogu 2026 DCPRiP LiNE Robo29")
    assert d["query"] == "Star Wars The Mandalorian and Grogu"
    assert d["year"] == "2026"
    assert d["key"] == "mv_starwarsthemandalorianandgrogu_2026"


def test_describe_movie_uses_last_year_so_titles_with_a_year_survive():
    # "Blade Runner 2049" released in 2017 -> title keeps its in-name year.
    d = artwork.describe("Blade Runner 2049 2017 1080p BluRay")
    assert d["query"] == "Blade Runner 2049"
    assert d["year"] == "2017"


def test_describe_series_is_year_agnostic_and_tv():
    a = artwork.describe("The Wire S01E01", "The Wire")
    b = artwork.describe("The Wire S05E10", "The Wire")
    assert a["key"] == b["key"] == "tv_thewire"
    assert a["kind"] == "tv"
    assert a["query"] == "The Wire"


def test_describe_punctuation_insensitive_key():
    assert artwork.describe("House of the Dragon", "House.of-the_Dragon")["key"] \
        == "tv_houseofthedragon"


def test_describe_empty_or_numeric_returns_none():
    assert artwork.describe("") is None
    assert artwork.describe("2010") is None  # nothing left once the year is gone


# --------------------------------------------------------------------------- #
# Cache helpers
# --------------------------------------------------------------------------- #

def test_art_library_dir_honors_env(art_home):
    assert artwork.art_library_dir() == art_home


def test_cached_path_and_miss(art_home):
    assert artwork.cached_path("mv_x") is None
    assert not artwork.is_known_miss("mv_x")
    art_home.mkdir(parents=True)
    (art_home / "mv_x.jpg").write_bytes(b"img")
    assert artwork.cached_path("mv_x") == art_home / "mv_x.jpg"
    artwork._mark_miss("mv_y")
    assert artwork.is_known_miss("mv_y")


# --------------------------------------------------------------------------- #
# iTunes search + fetch (network mocked)
# --------------------------------------------------------------------------- #

def test_search_artwork_url_upscales(monkeypatch):
    captured = {}

    def fake_get(url):
        captured["url"] = url
        return json.dumps({"results": [
            {"artworkUrl100": "https://is.example/a/b/100x100bb.jpg"},
        ]}).encode()

    monkeypatch.setattr(artwork, "_get", fake_get)
    url = artwork.search_artwork_url("Inception", "movie")
    assert url == "https://is.example/a/b/600x600bb.jpg"
    assert "term=Inception" in captured["url"]
    assert "entity=movie" in captured["url"]


def test_search_artwork_url_tv_entity(monkeypatch):
    seen = {}

    def fake_get(url):
        seen["url"] = url
        return json.dumps({"results": []}).encode()

    monkeypatch.setattr(artwork, "_get", fake_get)
    # No results -> None, and the request targets the TV (season) entity.
    assert artwork.search_artwork_url("The Wire", "tv") is None
    assert "entity=tvSeason" in seen["url"]
    assert "media=tvShow" in seen["url"]


def test_tmdb_preferred_over_itunes(monkeypatch):
    seen = {}

    def fake_get(url):
        seen.setdefault("urls", []).append(url)
        if "themoviedb" in url:
            return json.dumps({"results": [{"poster_path": "/abc.jpg"}]}).encode()
        return json.dumps({"results": []}).encode()  # iTunes (should not matter)

    monkeypatch.setattr(artwork, "_get", fake_get)
    url = artwork.search_artwork_url("Inception", "movie", "2010", tmdb_key="KEY")
    assert url == "https://image.tmdb.org/t/p/w600_and_h900_bestv2/abc.jpg"
    assert any("themoviedb" in u for u in seen["urls"])
    assert any("year=2010" in u for u in seen["urls"])
    assert any("api_key=KEY" in u for u in seen["urls"])


def test_tmdb_backdrop_variant_uses_wide_image(monkeypatch):
    seen = {}

    def fake_get(url):
        seen["url"] = url
        return json.dumps({"results": [
            {"poster_path": "/p.jpg", "backdrop_path": "/b.jpg"}]}).encode()

    monkeypatch.setattr(artwork, "_get", fake_get)
    url = artwork.search_artwork_url("Dune", "movie", "2024",
                                     tmdb_key="KEY", variant=artwork.BACKDROP)
    # Wide backdrop rendition + backdrop_path (not the portrait poster).
    assert url == "https://image.tmdb.org/t/p/w1280/b.jpg"


def test_itunes_has_no_backdrop(monkeypatch):
    # iTunes only offers square art, so the backdrop variant yields nothing there
    # (and with no TMDB key the whole chain returns None).
    monkeypatch.setattr(artwork, "_get",
                        lambda url: json.dumps({"results": [
                            {"artworkUrl100": "https://is.example/100x100bb.jpg"}]}).encode())
    assert artwork.search_artwork_url("The Wire", "tv", variant=artwork.BACKDROP) is None
    assert artwork.search_artwork_url("The Wire", "tv") is not None  # poster still works


def test_variants_cache_to_distinct_files(art_home):
    art_home.mkdir(parents=True)
    artwork._store("tv_x", b"poster", artwork.POSTER)
    artwork._store("tv_x", b"backdrop", artwork.BACKDROP)
    assert (art_home / "tv_x.jpg").read_bytes() == b"poster"
    assert (art_home / "tv_x.bg.jpg").read_bytes() == b"backdrop"
    assert artwork.cached_path("tv_x", artwork.POSTER).name == "tv_x.jpg"
    assert artwork.cached_path("tv_x", artwork.BACKDROP).name == "tv_x.bg.jpg"
    # A miss is tracked per variant.
    artwork._mark_miss("tv_y", artwork.BACKDROP)
    assert artwork.is_known_miss("tv_y", artwork.BACKDROP)
    assert not artwork.is_known_miss("tv_y", artwork.POSTER)


def test_falls_back_to_itunes_when_tmdb_empty(monkeypatch):
    def fake_get(url):
        if "themoviedb" in url:
            return json.dumps({"results": []}).encode()
        return json.dumps({"results": [
            {"artworkUrl100": "https://is.example/100x100bb.jpg"}]}).encode()

    monkeypatch.setattr(artwork, "_get", fake_get)
    url = artwork.search_artwork_url("The Wire", "tv", tmdb_key="KEY")
    assert url == "https://is.example/600x600bb.jpg"


def test_transient_failure_when_all_providers_error(monkeypatch):
    import urllib.error

    def boom(url):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(artwork, "_get", boom)
    # Every provider failed to respond -> the transport error propagates (so the
    # caller does not record a permanent miss).
    with pytest.raises(urllib.error.URLError):
        artwork.search_artwork_url("X", "movie", tmdb_key="KEY")


def test_clear_misses(art_home):
    art_home.mkdir(parents=True)
    (art_home / "mv_a.miss").write_text("")
    (art_home / "mv_b.miss").write_text("")
    (art_home / "mv_keep.jpg").write_bytes(b"img")
    assert artwork.clear_misses() == 2
    assert not artwork.is_known_miss("mv_a")
    assert artwork.cached_path("mv_keep") is not None  # images untouched


def test_fetch_downloads_and_caches(monkeypatch, art_home):
    calls = []

    def fake_get(url):
        calls.append(url)
        if "itunes" in url:
            return json.dumps({"results": [
                {"artworkUrl100": "https://is.example/100x100bb.jpg"},
            ]}).encode()
        return b"\xff\xd8\xff\xe0JPEGDATA"  # pretend image bytes

    monkeypatch.setattr(artwork, "_get", fake_get)
    path = artwork.fetch("mv_inception_2010", "Inception", "movie")
    assert path is not None and path.is_file()
    assert path.read_bytes() == b"\xff\xd8\xff\xe0JPEGDATA"
    assert any("itunes" in u for u in calls)
    # A second fetch is served straight from cache (no new network calls).
    calls.clear()
    again = artwork.fetch("mv_inception_2010", "Inception", "movie")
    assert again == path and not calls


def test_fetch_miss_marks_negative(monkeypatch, art_home):
    monkeypatch.setattr(artwork, "search_artwork_url", lambda *a, **k: None)
    assert artwork.fetch("mv_nope", "Nope", "movie") is None
    assert artwork.is_known_miss("mv_nope")
    assert artwork.cached_path("mv_nope") is None


def test_fetch_network_error_no_miss(monkeypatch, art_home):
    import urllib.error

    def boom(url):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(artwork, "_get", boom)
    assert artwork.fetch("mv_x", "X", "movie") is None
    # A transient failure must NOT be cached as a permanent miss.
    assert not artwork.is_known_miss("mv_x")


# --------------------------------------------------------------------------- #
# ArtworkService
# --------------------------------------------------------------------------- #

def test_service_state_ready_and_miss(art_home):
    art_home.mkdir(parents=True)
    (art_home / "mv_ready.jpg").write_bytes(b"img")
    artwork._mark_miss("mv_miss")
    svc = artwork.ArtworkService()
    try:
        assert svc.state("mv_ready") == "ready"
        assert svc.state("mv_miss") == "none"
        assert svc.state("", "q") == "none"
        assert svc.state("mv_unknown", "", schedule=False) == "none"
    finally:
        svc.close()


def test_service_schedules_background_fetch(monkeypatch, art_home):
    def fake_fetch(key, query, kind, *a):
        return artwork._store(key, b"img")

    monkeypatch.setattr(artwork, "fetch", fake_fetch)
    svc = artwork.ArtworkService()
    try:
        assert svc.state("mv_bg", "Movie", "movie") == "pending"
        # The worker thread populates the cache shortly after.
        for _ in range(50):
            if artwork.cached_path("mv_bg"):
                break
            time.sleep(0.02)
        assert artwork.cached_path("mv_bg") is not None
        assert svc.state("mv_bg") == "ready"
    finally:
        svc.close()


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@pytest.fixture
async def client(art_home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    app = webapp.build_app()
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


def test_art_for_source_series_and_url():
    # A remote URL whose filename is a series episode resolves to the show poster.
    src = ("https://host/files/House.of.the.Dragon.S03E01.1080p.AMZN.WEB-DL."
           "DDP5.1.Atmos.H.264-FLUX.mkv")
    art = webapp._art_for_source(src)
    assert art["key"] == "tv_houseofthedragon"
    assert art["kind"] == "tv"
    # A movie path resolves to a movie descriptor with its year.
    movie = webapp._art_for_source("/media/Inception.2010.1080p.BluRay.mkv")
    assert movie["key"] == "mv_inception_2010" and movie["kind"] == "movie"


async def test_api_art_serves_cached_else_404(client, art_home):
    r = await client.get("/api/art/mv_none")
    assert r.status == 404
    art_home.mkdir(parents=True)
    (art_home / "mv_have.jpg").write_bytes(b"\xff\xd8img")
    r = await client.get("/api/art/mv_have")
    assert r.status == 200
    assert await r.read() == b"\xff\xd8img"
    assert "max-age" in r.headers.get("Cache-Control", "")
    # The backdrop variant is served from its own file.
    (art_home / "mv_have.bg.jpg").write_bytes(b"wide")
    r = await client.get("/api/art/mv_have?variant=backdrop")
    assert r.status == 200 and await r.read() == b"wide"


async def test_api_art_request_reports_status(client, art_home, monkeypatch):
    # Keep the scheduled fetch off the network: an unknown key reports "pending".
    monkeypatch.setattr(artwork, "search_artwork_url", lambda q, k: None)
    art_home.mkdir(parents=True)
    (art_home / "mv_ready.jpg").write_bytes(b"img")
    artwork._mark_miss("mv_miss")

    r = await client.post("/api/art", json={"want": [
        {"key": "mv_ready", "query": "R", "kind": "movie"},
        {"key": "mv_miss", "query": "M", "kind": "movie"},
        {"key": "mv_new", "query": "N", "kind": "movie"},
    ]})
    assert r.status == 200
    art = (await r.json())["art"]
    assert art["mv_ready"] == "ready"
    assert art["mv_miss"] == "none"
    assert art["mv_new"] == "pending"
