from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from app.core.config import MAX_DURATION_SEC
from app.core.models import Job, JobCancelled
from app.core.persistence import save_job
from app.core.registry import remove as registry_remove
from app.pipeline.analyze import analyze
from app.pipeline.collect import cleanup_source, collect, sweep_old_jobs
from app.pipeline.download import _set, download
from app.pipeline.separate import separate

logger = logging.getLogger("stemdeck.pipeline")

# Only one heavy job runs at a time -- separation is GPU/CPU-hungry.
_pipeline_lock = asyncio.Semaphore(1)


def _record_stats(job: Job, status: str) -> None:
    try:
        from app.core.stats import record_completion
        record_completion(job.id, job.title, status)
    except Exception:
        # Stats must never fail a job, but a broken stats file should be seen.
        logger.exception("recording job stats failed for job %s", job.id)


def _check_cancel(job: Job) -> None:
    if job.cancel_requested:
        raise JobCancelled()


def _process(job: Job, source: Path, job_dir: Path) -> None:
    """Shared tail of both entry points: analyze, separate, collect."""
    analyze(job, source)
    _check_cancel(job)
    stems_root = separate(job, source, job_dir)
    found = collect(stems_root, job_dir)
    # The source (100-300 MB) is not used after separation.
    cleanup_source(job_dir)
    job.stems = [{"name": name, "url": f"/api/jobs/{job.id}/stems/{name}.wav"} for name in found]
    _check_cancel(job)


def _run_blocking(job: Job, url: str, job_dir: Path) -> None:
    _check_cancel(job)
    source = download(job, url, job_dir)
    _check_cancel(job)
    _process(job, source, job_dir)


def _run_blocking_from_file(job: Job, source: Path, job_dir: Path) -> None:
    _check_cancel(job)
    job.duration_sec = _validate_audio(source)
    _process(job, source, job_dir)


async def _run(
    job: Job,
    jobs_dir: Path,
    first_status: str,
    first_stage: str,
    blocking: Callable[..., None],
    arg: object,
) -> None:
    job_dir = jobs_dir / job.id
    # One try/except covers everything from directory creation through pipeline
    # execution. If anything before the lock raises, the job would otherwise
    # stay stuck on `queued` forever -- transition to `error` instead.
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        # Sweep stale jobs before waiting for the lock so disk reclaim happens
        # even while another job is running. A failing sweep must not fail
        # this job, but it must be visible.
        try:
            await asyncio.to_thread(sweep_old_jobs, jobs_dir)
        except Exception:
            logger.exception("sweep_old_jobs failed; old jobs are not being cleaned up")
        async with _pipeline_lock:
            # Leave "queued" in the same event-loop step that takes the lock:
            # cancel_job treats "queued" as "not started" (see app/api/jobs.py).
            _check_cancel(job)
            _set(job, status=first_status, progress=0.0, stage=first_stage)
            await asyncio.to_thread(blocking, job, arg, job_dir)
    except Exception as e:
        # yt-dlp wraps hook exceptions in DownloadError, so a cancel can arrive
        # as a generic exception -- the flag decides.
        if isinstance(e, JobCancelled) or job.cancel_requested:
            logger.info("pipeline cancelled for job %s", job.id)
            _set(job, status="cancelled", stage="Cancelled")
            # Drop partial files so the disk reclaim is immediate.
            shutil.rmtree(job_dir, ignore_errors=True)
            registry_remove(job.id)
            return
        logger.exception("pipeline failed for job %s", job.id)
        _set(job, status="error", stage=f"Error: {e}", error=str(e))
        shutil.rmtree(job_dir, ignore_errors=True)
        _record_stats(job, "error")
        return
    _set(job, status="done", progress=1.0, stage="Done")
    _record_stats(job, "done")
    save_job(job)


async def run_pipeline(job: Job, url: str, jobs_dir: Path) -> None:
    await _run(job, jobs_dir, "downloading", "Processing...", _run_blocking, url)


async def run_pipeline_from_file(job: Job, source: Path, jobs_dir: Path) -> None:
    await _run(job, jobs_dir, "analyzing", "Analyzing...", _run_blocking_from_file, source)


def _validate_audio(source: Path) -> float:
    """Validate that source is a readable audio file and return its duration in seconds."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams", "-select_streams", "a",
            str(source),
        ],
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise ValueError("Uploaded file is not a valid audio file")
    try:
        info = json.loads(result.stdout)
        if not info.get("streams"):
            raise ValueError("Uploaded file contains no audio stream")
        fmt = info.get("format", {})
        dur_str = fmt.get("duration") or info["streams"][0].get("duration")
    except (KeyError, TypeError, ValueError):
        raise ValueError("Uploaded file is not a valid audio file")
    if dur_str is None:
        raise ValueError("Audio duration is unknown -- cannot verify duration limit")
    try:
        dur = float(dur_str)
    except (ValueError, TypeError):
        raise ValueError("Audio duration is unknown -- cannot verify duration limit")
    if dur > MAX_DURATION_SEC:
        mins = MAX_DURATION_SEC // 60
        raise ValueError(f"Duration {int(dur // 60)} min exceeds limit of {mins} min")
    return dur
