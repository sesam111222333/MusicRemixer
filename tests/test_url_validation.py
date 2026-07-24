from __future__ import annotations

import pytest

from app.pipeline.download import InvalidYouTubeURL, validate_youtube_url


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PLfoo",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "  https://www.youtube.com/watch?v=dQw4w9WgXcQ  ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        # RD playlist variants — seed video ID follows the prefix, not always at offset 2
        (
            "https://music.youtube.com/playlist?list=RDAMVMdQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://music.youtube.com/playlist?list=RDEMdQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://music.youtube.com/playlist?list=RDQMdQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://music.youtube.com/playlist?list=RDCLAKdQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        # Plain RD (offset 2) must still work
        (
            "https://music.youtube.com/playlist?list=RDdQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        # Shorts URLs
        (
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://m.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        # Embed URLs
        (
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
    ],
)
def test_accepts_youtube_urls(url: str, expected: str) -> None:
    assert validate_youtube_url(url) == expected


@pytest.mark.parametrize(
    "url,reason_substring",
    [
        ("", "required"),
        ("   ", "required"),
        ("not a url", "http"),
        ("ftp://youtube.com/watch?v=dQw4w9WgXcQ", "http"),
        ("https://example.com/foo", "unsupported host"),
        ("https://www.youtube.com/playlist?list=PLfoo", "video ID"),
        ("https://evil.com/watch?v=dQw4w9WgXcQ", "unsupported host"),
    ],
)
def test_rejects_bad_urls(url: str, reason_substring: str) -> None:
    with pytest.raises(InvalidYouTubeURL) as exc:
        validate_youtube_url(url)
    assert reason_substring in str(exc.value)
