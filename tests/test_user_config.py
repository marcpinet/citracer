"""Tests for citracer.user_config — persistent user-level config store."""
import json
import os
from pathlib import Path

import pytest

from citracer import user_config


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    """Redirect ~/.citracer to a tmp dir for every test in this file so
    we never touch the real user config."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    # Path.home() consults HOME on POSIX and USERPROFILE on Windows; some
    # cached attributes need to be reset on Windows. Easiest: monkeypatch
    # Path.home directly.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    yield tmp_path


class TestConfigPaths:
    def test_config_dir_created_on_demand(self, isolated_home):
        d = user_config.config_dir()
        assert d.exists()
        assert d.name == ".citracer"
        assert d.parent == isolated_home

    def test_config_file_path(self, isolated_home):
        f = user_config.config_file()
        assert f.parent == isolated_home / ".citracer"
        assert f.name == "config.json"


class TestLoadSave:
    def test_load_missing_returns_empty(self, isolated_home):
        assert user_config.load_config() == {}

    def test_save_then_load_roundtrip(self, isolated_home):
        user_config.save_config({"foo": "bar", "n": 42})
        assert user_config.load_config() == {"foo": "bar", "n": 42}

    def test_corrupt_file_returns_empty(self, isolated_home):
        f = user_config.config_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{not valid json", encoding="utf-8")
        assert user_config.load_config() == {}

    def test_save_is_atomic(self, isolated_home):
        # Save twice; the .tmp file should not linger
        user_config.save_config({"a": 1})
        user_config.save_config({"a": 2})
        files = list(user_config.config_dir().iterdir())
        assert all(f.suffix != ".tmp" for f in files)
        assert user_config.load_config() == {"a": 2}


class TestS2Key:
    def test_get_when_unset(self, isolated_home):
        assert user_config.get_s2_api_key() is None

    def test_set_then_get(self, isolated_home):
        user_config.set_s2_api_key("pypi-secret-token")
        assert user_config.get_s2_api_key() == "pypi-secret-token"

    def test_set_persists_across_loads(self, isolated_home):
        user_config.set_s2_api_key("first")
        user_config.set_s2_api_key("second")
        assert user_config.get_s2_api_key() == "second"

    def test_clear_when_unset(self, isolated_home):
        assert user_config.clear_s2_api_key() is False

    def test_clear_when_set(self, isolated_home):
        user_config.set_s2_api_key("to-be-removed")
        assert user_config.clear_s2_api_key() is True
        assert user_config.get_s2_api_key() is None

    def test_other_keys_preserved_on_clear(self, isolated_home):
        user_config.save_config({"s2_api_key": "x", "other_setting": 42})
        user_config.clear_s2_api_key()
        assert user_config.load_config() == {"other_setting": 42}

    def test_permissions_tightened_on_posix(self, isolated_home):
        if os.name == "nt":
            pytest.skip("POSIX permissions don't apply on Windows")
        user_config.set_s2_api_key("xxx")
        mode = user_config.config_file().stat().st_mode & 0o777
        assert mode == 0o600


class TestMaskSecret:
    def test_none(self):
        assert user_config.mask_secret(None) == "(unset)"

    def test_empty(self):
        assert user_config.mask_secret("") == "(unset)"

    def test_short_secret_fully_masked(self):
        assert user_config.mask_secret("abc") == "***"
        assert user_config.mask_secret("abcdefgh") == "********"

    def test_long_secret_partial_mask(self):
        masked = user_config.mask_secret("pypi-AgEIcHlwaS5vcmctoken")
        assert masked.startswith("pypi")
        assert masked.endswith("oken")
        assert "*" in masked
        assert len(masked) == len("pypi-AgEIcHlwaS5vcmctoken")
