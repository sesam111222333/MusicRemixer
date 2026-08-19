from __future__ import annotations

from unittest.mock import patch

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
        # RD playlist variants — only RD and RDAMVM embed a real seed video ID.
        (
            "https://music.youtube.com/playlist?list=RDAMVMdQw4w9WgXcQ",
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
        # Path-based video ID must win over RD-list seed when both are present.
        # youtu.be/<realId>?list=RD<seedId> — realId is in the path, seedId in list.
        (
            "https://youtu.be/dQw4w9WgXcQ?list=RDoHg5SJYRHA0",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        # /shorts/<realId>?list=RD<seedId>
        (
            "https://www.youtube.com/shorts/dQw4w9WgXcQ?list=RDoHg5SJYRHA0",
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
        # RDCLAK/RDEM/RDQM playlists embed no seed video ID — the chars after
        # the prefix are an opaque identifier, not an 11-char video ID.
        ("https://music.youtube.com/playlist?list=RDCLAK5uymZ2r3nBd8abc", "video ID"),
        ("https://music.youtube.com/playlist?list=RDEM5uymZ2r3nBd8abc", "video ID"),
        ("https://music.youtube.com/playlist?list=RDQM5uymZ2r3nBd8abc", "video ID"),
        # Contrived RDCLAKxxx/RDEMxxx/RDQMxxx with a valid-looking suffix also rejected.
        ("https://music.youtube.com/playlist?list=RDCLAKdQw4w9WgXcQ", "video ID"),
        ("https://music.youtube.com/playlist?list=RDEMdQw4w9WgXcQ", "video ID"),
        ("https://music.youtube.com/playlist?list=RDQMdQw4w9WgXcQ", "video ID"),
        # Malformed watch?v= — video ID too short, too long, or empty
        ("https://www.youtube.com/watch?v=abc", "video ID"),
        ("https://www.youtube.com/watch?v=toolongidthatexceeds11", "video ID"),
        ("https://www.youtube.com/watch?v=", "video ID"),
    ],
)
def test_rejects_bad_urls(url: str, reason_substring: str) -> None:
    with pytest.raises(InvalidYouTubeURL) as exc:
        validate_youtube_url(url)
    assert reason_substring in str(exc.value)


def test_validate_youtube_url_hostname_valueerror_is_wrapped() -> None:
    """Regression: parsed.hostname is read outside the urlparse try block.
    If .hostname raises ValueError (e.g. malformed bracketed IPv6), it must
    be caught and re-raised as InvalidYouTubeURL, not escape as a bare ValueError
    that bypasses the create_job handler and causes HTTP 500."""

    class _BadResult:
        scheme = "https"

        @property
        def hostname(self) -> str:
            raise ValueError("Invalid IPv6 URL")

    with patch("urllib.parse.urlparse", return_value=_BadResult()):
        with pytest.raises(InvalidYouTubeURL):
            validate_youtube_url("https://[::1")
