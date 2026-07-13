from __future__ import annotations

import subprocess
import threading

from app.core.models import Job

_jobs: dict[str, Job] = {}
# Active subprocesses keyed by job_id (currently only Demucs). Lets
# POST /cancel terminate the running process from the API thread instead
# of waiting for the pipeline thread to notice the cancel flag.
_procs: dict[str, subprocess.Popen] = {}
# Count of in-flight HTTP requests currently reading a job's files (stems,
# zip, remix). sweep_old_jobs checks this before deleting a job directory to
# avoid evicting files that are actively being streamed.
_readers: dict[str, int] = {}
# Job IDs currently being deleted by sweep_old_jobs. inc_readers checks this
# under _lock so it never increments after sweep has claimed the job for
# deletion — closing the TOCTOU window between has_readers and rmtree.
_sweeping: set[str] = set()
# Protects all _readers and _sweeping mutations so concurrent FastAPI thread-pool
# requests can't lose increments via non-atomic read-modify-write.
_lock = threading.Lock()


def register(job: Job) -> Job:
    _jobs[job.id] = job
    return job


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def remove(job_id: str) -> None:
    _jobs.pop(job_id, None)
    _procs.pop(job_id, None)


def all_jobs() -> dict[str, Job]:
    """Return a snapshot of the registry for sweep / cleanup."""
    return dict(_jobs)


def set_proc(job_id: str, proc: subprocess.Popen | None) -> None:
    if proc is None:
        _procs.pop(job_id, None)
    else:
        _procs[job_id] = proc


def get_proc(job_id: str) -> subprocess.Popen | None:
    return _procs.get(job_id)


def inc_readers(job_id: str) -> bool:
    """Increment the reader count for job_id.

    Returns True on success, False if sweep has already claimed the job for
    deletion.  Callers must 404 immediately when False is returned — the job
    directory is in the process of being deleted.
    """
    with _lock:
        if job_id in _sweeping:
            return False
        _readers[job_id] = _readers.get(job_id, 0) + 1
        return True


def dec_readers(job_id: str) -> None:
    with _lock:
        count = _readers.get(job_id, 0) - 1
        if count <= 0:
            _readers.pop(job_id, None)
        else:
            _readers[job_id] = count


def has_readers(job_id: str) -> bool:
    return _readers.get(job_id, 0) > 0


def claim_for_sweep(job_id: str) -> bool:
    """Atomically check that no readers are active and mark the job as sweeping.

    Returns True if the claim was acquired (safe to rmtree), False if there are
    active readers (sweep must defer).  While a claim is held, inc_readers returns
    False, preventing new readers from opening files that are being deleted.
    Always pair with release_sweep_claim() in a try/finally block.
    """
    with _lock:
        if _readers.get(job_id, 0) > 0:
            return False
        _sweeping.add(job_id)
        return True


def release_sweep_claim(job_id: str) -> None:
    """Release the sweep claim acquired by claim_for_sweep."""
    with _lock:
        _sweeping.discard(job_id)
