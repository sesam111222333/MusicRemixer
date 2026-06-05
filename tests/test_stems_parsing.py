from __future__ import annotations

from app.api.stems import _parse_pitch


def test_pitch_inf_does_not_overflow():
    """round(float('inf')) raises OverflowError, not ValueError.

    The except ValueError in download_remix does not catch it, so the whole
    request crashes with HTTP 500. Both +inf and -inf must fall back to 0,
    just like any other non-numeric input.
    """
    assert _parse_pitch("inf") == 0
    assert _parse_pitch("-inf") == 0


def test_pitch_valid_values_clamped():
    assert _parse_pitch("0") == 0
    assert _parse_pitch("5") == 5
    assert _parse_pitch("-5") == -5
    assert _parse_pitch("100") == 12
    assert _parse_pitch("-100") == -12


def test_pitch_invalid_string_falls_back_to_zero():
    assert _parse_pitch("notanumber") == 0
    assert _parse_pitch("") == 0
