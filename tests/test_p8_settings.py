"""P8 — persisted settings (load/save/defaults + system language)."""

from __future__ import annotations

import json

import pytest

from vidstreamer import settings as st


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Point settings storage at an isolated XDG config home."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_defaults_when_missing(cfg_home, monkeypatch):
    monkeypatch.chdir(cfg_home)
    s = st.load_settings()
    assert s.media_root == str(cfg_home)
    assert s.preferred_sub_lang  # non-empty 2-letter code
    assert len(s.preferred_sub_lang) == 2
    assert s.opensubtitles_api_key == ""
    # Loading must not create the file.
    assert not st.settings_path().exists()


def test_save_creates_dir_and_roundtrips(cfg_home):
    s = st.Settings(media_root="/movies", preferred_sub_lang="cs",
                    opensubtitles_api_key="secret")
    st.save_settings(s)
    path = st.settings_path()
    assert path.exists()
    assert path.parent.name == "vidstreamer"
    loaded = st.load_settings()
    assert loaded.media_root == "/movies"
    assert loaded.preferred_sub_lang == "cs"
    assert loaded.opensubtitles_api_key == "secret"


def test_corrupt_file_returns_defaults(cfg_home, monkeypatch):
    monkeypatch.chdir(cfg_home)
    path = st.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    s = st.load_settings()  # must not raise
    assert s.media_root == str(cfg_home)


def test_update_settings_persists_and_merges(cfg_home):
    st.save_settings(st.Settings(media_root="/a", preferred_sub_lang="en",
                                 opensubtitles_api_key="k1"))
    updated = st.update_settings(opensubtitles_api_key="k2")
    assert updated.opensubtitles_api_key == "k2"
    assert updated.media_root == "/a"  # untouched field preserved
    # Persisted to disk.
    on_disk = json.loads(st.settings_path().read_text(encoding="utf-8"))
    assert on_disk["opensubtitles_api_key"] == "k2"


def test_update_ignores_unknown_and_none(cfg_home):
    st.update_settings(bogus="x", preferred_sub_lang=None, media_root="/b")
    loaded = st.load_settings()
    assert loaded.media_root == "/b"
    assert not hasattr(loaded, "bogus")


def test_empty_persisted_fields_fall_back_to_defaults(cfg_home, monkeypatch):
    monkeypatch.chdir(cfg_home)
    path = st.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"media_root": "", "preferred_sub_lang": ""}),
                    encoding="utf-8")
    s = st.load_settings()
    assert s.media_root == str(cfg_home)
    assert len(s.preferred_sub_lang) == 2


def test_system_language_shape(monkeypatch):
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.delenv("LANGUAGE", raising=False)
    # Force the locale paths to yield nothing usable so env is consulted.
    monkeypatch.setattr(st.locale, "getlocale", lambda *a: (None, None))
    monkeypatch.setattr(st.locale, "getdefaultlocale", lambda *a: (None, None))
    assert st.system_language() == "de"


def test_system_language_falls_back_to_en(monkeypatch):
    for var in ("LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(st.locale, "getlocale", lambda *a: (None, None))
    monkeypatch.setattr(st.locale, "getdefaultlocale", lambda *a: (None, None))
    assert st.system_language() == "en"
