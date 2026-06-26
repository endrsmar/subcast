"""P9 — subtitle search: sidecar, media-root scan, online (mocked network)."""

from __future__ import annotations

import io
import json

import pytest

from vidstreamer import subsearch
from vidstreamer.errors import SubSearchError


# --------------------------------------------------------------------------- #
# Tier 1: find_sidecar
# --------------------------------------------------------------------------- #

def test_find_sidecar_exact_stem(tmp_path):
    (tmp_path / "Movie.mkv").write_text("v")
    sub = tmp_path / "Movie.srt"
    sub.write_text("s")
    found = subsearch.find_sidecar(str(tmp_path / "Movie.mkv"))
    assert found == str(sub.resolve())


def test_find_sidecar_prefers_language(tmp_path):
    (tmp_path / "Movie.mkv").write_text("v")
    (tmp_path / "Movie.en.srt").write_text("en")
    cs = tmp_path / "Movie.cs.srt"
    cs.write_text("cs")
    found = subsearch.find_sidecar(str(tmp_path / "Movie.mkv"), preferred_lang="cs")
    assert found == str(cs.resolve())


def test_find_sidecar_prefers_srt_over_vtt(tmp_path):
    (tmp_path / "Movie.mkv").write_text("v")
    srt = tmp_path / "Movie.srt"
    srt.write_text("s")
    (tmp_path / "Movie.vtt").write_text("v")
    found = subsearch.find_sidecar(str(tmp_path / "Movie.mkv"))
    assert found == str(srt.resolve())


def test_find_sidecar_none_when_absent(tmp_path):
    (tmp_path / "Movie.mkv").write_text("v")
    (tmp_path / "Other.srt").write_text("s")
    assert subsearch.find_sidecar(str(tmp_path / "Movie.mkv")) is None


# --------------------------------------------------------------------------- #
# Tier 2: search_media_root
# --------------------------------------------------------------------------- #

def test_search_media_root_finds_nested_match(tmp_path):
    video = tmp_path / "The.Big.Movie.2021.mkv"
    video.write_text("v")
    subdir = tmp_path / "subs"
    subdir.mkdir()
    match = subdir / "The Big Movie 2021.srt"
    match.write_text("s")
    (subdir / "Unrelated.srt").write_text("x")
    results = subsearch.search_media_root(str(video), str(tmp_path))
    assert results
    assert results[0]["path"] == str(match.resolve()) or \
        results[0]["name"] == "The Big Movie 2021.srt"
    names = [r["name"] for r in results]
    assert "Unrelated.srt" not in names


def test_search_media_root_skips_hidden_dirs(tmp_path):
    video = tmp_path / "Show.mkv"
    video.write_text("v")
    hidden = tmp_path / ".cache"
    hidden.mkdir()
    (hidden / "Show.srt").write_text("s")
    results = subsearch.search_media_root(str(video), str(tmp_path))
    assert results == []


def test_search_media_root_language_boosts_score(tmp_path):
    video = tmp_path / "Film.mkv"
    video.write_text("v")
    (tmp_path / "Film.en.srt").write_text("en")
    (tmp_path / "Film.cs.srt").write_text("cs")
    results = subsearch.search_media_root(str(video), str(tmp_path),
                                          preferred_lang="cs")
    assert results[0]["lang"] == "cs"


def test_search_media_root_missing_root(tmp_path):
    assert subsearch.search_media_root(str(tmp_path / "x.mkv"),
                                       str(tmp_path / "nope")) == []


# --------------------------------------------------------------------------- #
# Tier 3: online search / download (network mocked)
# --------------------------------------------------------------------------- #

class _FakeResp:
    def __init__(self, payload):
        self._data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_search_online_requires_api_key():
    with pytest.raises(SubSearchError) as exc:
        subsearch.search_online("Movie.mkv", "en", "")
    assert "api key" in str(exc.value).lower()


def test_search_online_parses_results(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["api_key"] = req.headers.get("Api-key")
        captured["ua"] = req.headers.get("User-agent")
        return _FakeResp({
            "data": [
                {"id": "1", "attributes": {
                    "language": "en", "release": "BluRay", "download_count": 99,
                    "files": [{"file_id": 555, "file_name": "Movie.en.srt"}],
                }},
            ],
        })

    monkeypatch.setattr(subsearch.urllib.request, "urlopen", fake_urlopen)
    results = subsearch.search_online("/m/Movie.mkv", "en", "KEY")
    assert len(results) == 1
    r = results[0]
    assert r["lang"] == "en"
    assert r["file_id"] == 555
    assert r["download_count"] == 99
    assert captured["api_key"] == "KEY"
    assert "vidstreamer" in captured["ua"]
    assert "query=Movie" in captured["url"]


def test_search_online_sorted_by_similarity(monkeypatch):
    """Online results are ranked by filename similarity, not API order."""
    def fake_urlopen(req, timeout=None):
        return _FakeResp({
            "data": [
                # Listed worst-match first; a high download count must NOT float
                # an unrelated subtitle above a closely-matching one.
                {"id": "a", "attributes": {
                    "language": "en", "release": "Some Other Movie",
                    "download_count": 99999,
                    "files": [{"file_id": 1, "file_name": "Some.Other.Movie.srt"}],
                }},
                {"id": "b", "attributes": {
                    "language": "en", "release": "BluRay", "download_count": 5,
                    "files": [{"file_id": 2,
                               "file_name": "The.Big.Movie.2021.1080p.srt"}],
                }},
            ],
        })

    monkeypatch.setattr(subsearch.urllib.request, "urlopen", fake_urlopen)
    results = subsearch.search_online("/m/The.Big.Movie.2021.1080p.mkv", "en", "KEY")
    assert [r["file_id"] for r in results] == [2, 1]
    assert results[0]["score"] > results[1]["score"]


def test_search_online_network_error_wrapped(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(subsearch.urllib.request, "urlopen", boom)
    with pytest.raises(SubSearchError):
        subsearch.search_online("Movie.mkv", "en", "KEY")


def test_download_online_saves_file(monkeypatch, tmp_path):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url.endswith("/download"):
            return _FakeResp({"link": "https://cdn.example/sub.srt",
                              "file_name": "Movie.en.srt"})
        return _FakeResp(b"1\n00:00:01,000 --> 00:00:02,000\nhi\n")

    monkeypatch.setattr(subsearch.urllib.request, "urlopen", fake_urlopen)
    dest = subsearch.download_online("555", "KEY", str(tmp_path))
    assert dest.endswith("Movie.en.srt")
    assert "hi" in open(dest, encoding="utf-8").read()
    assert any(u.endswith("/download") for u in calls)


def test_download_online_requires_link(monkeypatch, tmp_path):
    monkeypatch.setattr(subsearch.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp({}))
    with pytest.raises(SubSearchError):
        subsearch.download_online("555", "KEY", str(tmp_path))


def test_download_online_requires_api_key(tmp_path):
    with pytest.raises(SubSearchError):
        subsearch.download_online("555", "", str(tmp_path))
