from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.config import MAX_DURATION_SEC
from app.core.models import Job, JobCancelled
from app.pipeline.runner import _validate_audio, run_pipeline


@pytest.mark.asyncio
async def test_pipeline_transitions_to_error_on_stage_failure(tmp_path: Path):
    job = Job(id="abcdefabcdef")

    def boom(*args, **kwargs):
        raise RuntimeError("download blew up")

    with patch("app.pipeline.runner._run_blocking", side_effect=boom):
        await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)

    assert job.status == "error"
    assert "blew up" in (job.error or "")


@pytest.mark.asyncio
async def test_pipeline_marks_done_on_success(tmp_path: Path):
    job = Job(id="abcdefabcdee")

    with patch("app.pipeline.runner._run_blocking", return_value=None):
        await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)

    assert job.status == "done"
    assert job.progress == 1.0


@pytest.mark.asyncio
async def test_pipeline_handles_jobcancelled(tmp_path: Path):
    job = Job(id="abcdefabcdec")
    job.cancel_requested = True

    def cancel(*args, **kwargs):
        raise JobCancelled()

    with patch("app.pipeline.runner._run_blocking", side_effect=cancel):
        await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)

    assert job.status == "cancelled"
    # Partial job dir is removed.
    assert not (tmp_path / job.id).exists()


@pytest.mark.asyncio
async def test_pipeline_handles_wrapped_cancel(tmp_path: Path):
    """yt-dlp wraps hook exceptions in DownloadError; the runner must still
    treat it as a cancel when the flag is set."""
    job = Job(id="abcdefabcdeb")
    job.cancel_requested = True

    def wrapped(*args, **kwargs):
        raise RuntimeError("yt-dlp DownloadError wrapping JobCancelled")

    with patch("app.pipeline.runner._run_blocking", side_effect=wrapped):
        await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)

    assert job.status == "cancelled"


@pytest.mark.asyncio
async def test_pipeline_recovers_from_mkdir_failure(tmp_path: Path):
    """If something pre-lock raises, the job must transition to error
    instead of staying stuck on `queued`."""
    job = Job(id="abcdefabcdea")
    bad_jobs_dir = tmp_path / "blocked"
    # Make jobs_dir a regular file so mkdir(parents=True) under it raises.
    bad_jobs_dir.write_bytes(b"not a directory")

    await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", bad_jobs_dir)

    assert job.status == "error"


def test_validate_audio_rejects_duration_exceeding_limit(monkeypatch, tmp_path):
    """_validate_audio must raise ValueError when duration exceeds MAX_DURATION_SEC."""
    too_long = MAX_DURATION_SEC + 1

    fake_result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({
            "streams": [{"codec_type": "audio"}],
            "format": {"duration": str(float(too_long))},
        }).encode(),
        stderr=b"",
    )

    monkeypatch.setattr("app.pipeline.runner.subprocess.run", lambda *a, **kw: fake_result)

    audio_file = tmp_path / "audio.mp3"
    audio_file.write_bytes(b"fake")

    with pytest.raises(ValueError, match="[Dd]uration"):
        _validate_audio(audio_file)


def test_validate_audio_accepts_duration_within_limit(monkeypatch, tmp_path):
    """_validate_audio must return duration when it is within MAX_DURATION_SEC."""
    ok_duration = MAX_DURATION_SEC - 1

    fake_result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({
            "streams": [{"codec_type": "audio"}],
            "format": {"duration": str(float(ok_duration))},
        }).encode(),
        stderr=b"",
    )

    monkeypatch.setattr("app.pipeline.runner.subprocess.run", lambda *a, **kw: fake_result)

    audio_file = tmp_path / "audio.mp3"
    audio_file.write_bytes(b"fake")

    dur = _validate_audio(audio_file)
    assert dur == float(ok_duration)
