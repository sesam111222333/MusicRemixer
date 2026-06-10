from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.core.models import Job
from app.pipeline.download import download


def _make_job() -> Job:
    return Job(id="aabbccddeeff")


def _patch_ydl(meta: dict):
    """Patch YoutubeDL so extract_info returns *meta* (no network calls)."""
    mock_cls = MagicMock()
    mock_cls.return_value.__enter__.return_value.extract_info.return_value = meta
    return patch("app.pipeline.download.YoutubeDL", mock_cls)


def test_live_stream_is_rejected(tmp_path):
    """download() must raise RuntimeError for is_live=True instead of treating
    duration=None as 0 and bypassing the cap check."""
    with _patch_ydl({"is_live": True, "duration": None}):
        with pytest.raises(RuntimeError, match="[Ll]ive"):
            download(_make_job(), "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)


def test_unknown_duration_is_rejected(tmp_path):
    """When yt-dlp returns duration=None and is_live is falsy, download() must
    raise instead of treating 0 as within the cap."""
    with _patch_ydl({"is_live": False, "duration": None}):
        with pytest.raises(RuntimeError, match="[Dd]uration"):
            download(_make_job(), "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)
