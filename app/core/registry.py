from __future__ import annotations

import subprocess

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


def inc_readers(job_id: str) -> None:
    _readers[job_id] = _readers.get(job_id, 0) + 1


def dec_readers(job_id: str) -> None:
    count = _readers.get(job_id, 0) - 1
    if count <= 0:
        _readers.pop(job_id, None)
    else:
        _readers[job_id] = count


def has_readers(job_id: str) -> bool:
    return _readers.get(job_id, 0) > 0
