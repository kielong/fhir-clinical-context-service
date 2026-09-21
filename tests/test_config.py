# ORIGIN: AI — test cases typed by Claude Code, reviewed by Kiel. The model choice itself is the
#   bake-off's result (see eval/); these tests only keep every place that names it in agreement.
"""The default model is named in three places; they must never drift apart."""

import re
from pathlib import Path

import pytest

from clinical_context.config import Settings, get_settings

ROOT = Path(__file__).parent.parent
LOCKED_MODEL = "gemma3:4b"


def test_the_default_model_is_the_one_the_bake_off_chose():
    assert Settings(_env_file=None).ollama_model == LOCKED_MODEL


def test_the_example_env_file_names_the_same_model():
    text = (ROOT / ".env.example").read_text()

    assert re.search(rf"^OLLAMA_MODEL={re.escape(LOCKED_MODEL)}$", text, re.MULTILINE)


def test_compose_pulls_the_same_model_when_nothing_overrides_it():
    text = (ROOT / "docker-compose.yml").read_text()

    assert f"OLLAMA_MODEL: ${{OLLAMA_MODEL:-{LOCKED_MODEL}}}" in text


@pytest.fixture
def fresh_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()  # never leave a test's environment behind for the next test


def test_settings_are_read_from_the_environment_once_and_then_reused(
    monkeypatch, fresh_settings_cache
):
    monkeypatch.setenv("OLLAMA_MODEL", "first-model:1b")
    first = get_settings()
    monkeypatch.setenv("OLLAMA_MODEL", "second-model:1b")

    assert first.ollama_model == "first-model:1b"
    assert get_settings() is first  # the same object: the environment is not read again
