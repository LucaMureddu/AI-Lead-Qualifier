"""
tests/unit/test_config.py
-------------------------
Unit test per ``Settings``: validazione del provider e proprietà derivate.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.config import Settings, get_settings

pytestmark = pytest.mark.unit


def test_invalid_provider_raises() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_provider="not-a-provider")


def test_valid_providers_accepted() -> None:
    for provider in ("openai", "groq", "llama"):
        assert Settings(llm_provider=provider).llm_provider == provider


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
