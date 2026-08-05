"""Tests for settings persistence.

``config_path`` is monkeypatched to a temp file throughout, so nothing here
touches the real per-user config.
"""

import json

import pytest

from app import config as config_module
from app.config import load_config, save_config


@pytest.fixture(autouse=True)
def temp_config(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "config.json"
    monkeypatch.setattr(config_module, "config_path", lambda: path)
    return path


def test_missing_file_returns_empty_dict():
    assert load_config() == {}


def test_roundtrip(temp_config):
    save_config({"base_url": "https://confluence.example.com", "overwrite": True})
    assert load_config() == {
        "base_url": "https://confluence.example.com",
        "overwrite": True,
    }


def test_save_creates_missing_directories(temp_config):
    assert not temp_config.parent.exists()
    save_config({"a": 1})
    assert temp_config.is_file()


def test_corrupt_json_is_treated_as_absent(temp_config):
    temp_config.parent.mkdir(parents=True)
    temp_config.write_text("{not json", encoding="utf-8")
    assert load_config() == {}


def test_non_object_json_is_rejected(temp_config):
    temp_config.parent.mkdir(parents=True)
    temp_config.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_config() == {}


def test_save_replaces_previous_contents_entirely(temp_config):
    save_config({"pat": "secret", "base_url": "https://a.example.com"})
    # Un-ticking "Remember credentials" simply omits the key next time; the
    # rewrite-from-scratch behaviour is what erases it from disk.
    save_config({"base_url": "https://a.example.com"})
    assert load_config() == {"base_url": "https://a.example.com"}
    assert "secret" not in temp_config.read_text(encoding="utf-8")


def test_written_file_is_human_editable(temp_config):
    save_config({"b": 2, "a": 1})
    text = temp_config.read_text(encoding="utf-8")
    # Indented and key-sorted, because users are expected to open this file.
    assert text.startswith("{\n")
    assert text.index('"a"') < text.index('"b"')
    assert json.loads(text) == {"a": 1, "b": 2}


def test_no_temp_file_is_left_behind(temp_config):
    save_config({"a": 1})
    leftovers = list(temp_config.parent.glob("*.tmp"))
    assert leftovers == []


def test_unwritable_location_does_not_raise(monkeypatch, tmp_path):
    # A file where the parent directory should be: mkdir fails, and settings
    # persistence must never take the UI down with it.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        config_module, "config_path", lambda: blocker / "sub" / "config.json"
    )
    save_config({"a": 1})       # must not raise
    assert load_config() == {}
