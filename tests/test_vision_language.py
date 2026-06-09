"""Vision image descriptions follow the document / configured language.

German PDFs should get German image descriptions so they match German chat
queries and highlight; an explicit setting overrides the auto-detection.
"""

from __future__ import annotations

from app.services.indexer import _vision_prompt


def test_vision_prompt_german_is_german():
    p = _vision_prompt("de")
    assert "Deutsch" in p


def test_vision_prompt_english_default():
    p = _vision_prompt("en")
    assert "Describe this image" in p


def test_vision_prompt_unknown_falls_back_to_english():
    assert _vision_prompt("fr") == _vision_prompt("en")
    assert _vision_prompt(None) == _vision_prompt("en")
