from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.config import MAX_DURATION_SEC
from app.core.models import Job, JobCancelled
from app.core.registry import _jobs
from app.pipeline.runner import _validate_audio, run_pipeline, run_pipeline_from_file


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


def test_validate_audio_rejects_na_duration(monkeypatch, tmp_path):
    """_validate_audio must raise ValueError when ffprobe reports duration as 'N/A'.
    Such non-numeric strings are truthy and bypass the None-check but break float(),
    so the error must be caught and converted to the 'unknown' message."""
    fake_result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({
            "streams": [{"codec_type": "audio", "duration": "N/A"}],
            "format": {"duration": "N/A"},
        }).encode(),
        stderr=b"",
    )

    monkeypatch.setattr("app.pipeline.runner.subprocess.run", lambda *a, **kw: fake_result)

    audio_file = tmp_path / "audio.mp3"
    audio_file.write_bytes(b"fake")

    with pytest.raises(ValueError, match="unknown"):
        _validate_audio(audio_file)


def test_validate_audio_rejects_unknown_duration(monkeypatch, tmp_path):
    """_validate_audio must raise ValueError when ffprobe reports no duration.
    Containers like ADTS/.aac or certain .ogg/.opus/.wma omit duration from both
    the format and the first stream. Previously `or 0` silently bypassed the limit."""
    fake_result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({
            "streams": [{"codec_type": "audio"}],  # no "duration" key
            "format": {},                           # no "duration" key
        }).encode(),
        stderr=b"",
    )

    monkeypatch.setattr("app.pipeline.runner.subprocess.run", lambda *a, **kw: fake_result)

    audio_file = tmp_path / "audio.aac"
    audio_file.write_bytes(b"fake")

    with pytest.raises(ValueError, match="unknown"):
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


@pytest.mark.asyncio
async def test_pipeline_cleans_job_dir_on_error(tmp_path: Path):
    """On a non-cancel failure, run_pipeline must delete job_dir so that
    100-300 MB source files and temp separation dirs don't linger until TTL."""
    job = Job(id="errorcleanup1")

    def boom(*args, **kwargs):
        # Simulate demucs OOM: create some temp files first, then crash.
        job_dir = tmp_path / job.id
        (job_dir / "_demucs_tmp").mkdir(parents=True, exist_ok=True)
        (job_dir / "_demucs_tmp" / "partial.wav").write_bytes(b"x" * 1024)
        raise RuntimeError("CUDA out of memory")

    with patch("app.pipeline.runner._run_blocking", side_effect=boom):
        await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)

    assert job.status == "error"
    # job_dir and all its contents must be gone.
    assert not (tmp_path / job.id).exists(), "job_dir was not cleaned up on error"


@pytest.mark.asyncio
async def test_pipeline_from_file_cleans_job_dir_on_error(tmp_path: Path):
    """On a non-cancel failure, run_pipeline_from_file must delete job_dir."""
    job = Job(id="errorcleanup2")
    source = tmp_path / "source.wav"
    source.write_bytes(b"fake audio")

    def boom(*args, **kwargs):
        # Simulate a crash after separation dirs are created.
        job_dir = tmp_path / job.id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "_bsr_tmp").mkdir()
        (job_dir / "_bsr_tmp" / "partial.wav").write_bytes(b"x" * 1024)
        raise RuntimeError("audio-separator OOM")

    with patch("app.pipeline.runner._run_blocking_from_file", side_effect=boom):
        await run_pipeline_from_file(job, source, tmp_path)

    assert job.status == "error"
    assert not (tmp_path / job.id).exists(), "job_dir was not cleaned up on error"


@pytest.mark.asyncio
async def test_error_keeps_job_in_registry(tmp_path: Path):
    """After a pipeline failure the job must REMAIN in the registry so that
    GET /api/jobs/{id} returns status=error (with the error cause) instead of 404.
    Removing the job on the error path means any REST client that isn't holding
    a live SSE stream — including the reconnect probe and startJobPolling — will
    receive 404 and lose the error message forever."""
    job = Job(id="reglerr_00001")
    _jobs[job.id] = job
    try:
        with patch("app.pipeline.runner._run_blocking", side_effect=RuntimeError("download failed")):
            await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)
        assert job.id in _jobs, "job must remain in registry so clients can fetch the error status"
        assert _jobs[job.id].status == "error"
        assert "download failed" in (_jobs[job.id].error or "")
    finally:
        _jobs.pop(job.id, None)


@pytest.mark.asyncio
async def test_cancel_removes_job_from_registry(tmp_path: Path):
    """run_pipeline must call registry.remove on the cancelled path."""
    job = Job(id="regcanc_00001")
    job.cancel_requested = True
    _jobs[job.id] = job
    try:
        with patch("app.pipeline.runner._run_blocking", side_effect=JobCancelled()):
            await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)
        assert job.id not in _jobs
    finally:
        _jobs.pop(job.id, None)


@pytest.mark.asyncio
async def test_from_file_error_keeps_job_in_registry(tmp_path: Path):
    """After a run_pipeline_from_file failure the job must REMAIN in the registry
    so that GET /api/jobs/{id} returns status=error instead of 404."""
    job = Job(id="reglerr_00002")
    _jobs[job.id] = job
    source = tmp_path / "audio.wav"
    source.write_bytes(b"fake")
    try:
        with patch("app.pipeline.runner._run_blocking_from_file", side_effect=RuntimeError("audio invalid")):
            await run_pipeline_from_file(job, source, tmp_path)
        assert job.id in _jobs, "job must remain in registry so clients can fetch the error status"
        assert _jobs[job.id].status == "error"
        assert "audio invalid" in (_jobs[job.id].error or "")
    finally:
        _jobs.pop(job.id, None)


@pytest.mark.asyncio
async def test_sweep_oserror_does_not_fail_new_job(tmp_path: Path):
    """An OSError from sweep_old_jobs must NOT mark the new job as error.

    Regression: sweep_old_jobs was called inside the same try/except that routes
    all exceptions to job.status=error. A PermissionError on a stale orphan directory
    would spuriously fail and rmtree the just-submitted job's directory.
    """
    job = Job(id="sweepfail_0001")

    def bad_sweep(*args, **kwargs):
        raise PermissionError("[Errno 13] simulated stale NFS orphan")

    with patch("app.pipeline.runner.sweep_old_jobs", side_effect=bad_sweep):
        with patch("app.pipeline.runner._run_blocking", return_value=None):
            await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)

    assert job.status == "done", (
        f"expected 'done', got {job.status!r} — sweep_old_jobs error must not bleed into the job"
    )


@pytest.mark.asyncio
async def test_from_file_sweep_oserror_does_not_fail_new_job(tmp_path: Path):
    """Same as test_sweep_oserror_does_not_fail_new_job but for run_pipeline_from_file."""
    job = Job(id="sweepfail_0002")
    source = tmp_path / "audio.wav"
    source.write_bytes(b"fake audio")

    def bad_sweep(*args, **kwargs):
        raise PermissionError("[Errno 13] simulated stale NFS orphan")

    with patch("app.pipeline.runner.sweep_old_jobs", side_effect=bad_sweep):
        with patch("app.pipeline.runner._run_blocking_from_file", return_value=None):
            await run_pipeline_from_file(job, source, tmp_path)

    assert job.status == "done", (
        f"expected 'done', got {job.status!r} — sweep_old_jobs error must not bleed into the job"
    )


@pytest.mark.asyncio
async def test_from_file_cancel_removes_job_from_registry(tmp_path: Path):
    """run_pipeline_from_file must call registry.remove on the cancelled path."""
    job = Job(id="regcanc_00002")
    job.cancel_requested = True
    _jobs[job.id] = job
    source = tmp_path / "audio.wav"
    source.write_bytes(b"fake")
    try:
        with patch("app.pipeline.runner._run_blocking_from_file", side_effect=JobCancelled()):
            await run_pipeline_from_file(job, source, tmp_path)
        assert job.id not in _jobs
    finally:
        _jobs.pop(job.id, None)
