from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.models import Job
from app.core.persistence import load_all_jobs
from app.core.registry import _jobs, _readers, dec_readers, inc_readers
from app.pipeline.collect import sweep_old_jobs


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    _readers.clear()
    yield
    _jobs.clear()
    _readers.clear()


def _mkdir(jobs_dir: Path, name: str) -> Path:
    d = jobs_dir / name
    d.mkdir(parents=True)
    (d / "marker").write_bytes(b"x")
    return d


def test_skip_active_job_even_if_old(tmp_path: Path):
    """An active (non-terminal) job's directory must never be swept,
    even if its created_at predates the TTL cutoff."""
    d = _mkdir(tmp_path, "abcdefabcdef")
    job = Job(id="abcdefabcdef")
    job.status = "separating"
    job.created_at = time.time() - 999_999  # ancient
    _jobs[job.id] = job

    with patch("app.pipeline.collect.JOB_TTL_SECONDS", 60):
        sweep_old_jobs(tmp_path)

    assert d.is_dir()
    assert job.id in _jobs


def test_sweeps_terminal_old_job(tmp_path: Path):
    d = _mkdir(tmp_path, "abcdefabcdee")
    job = Job(id="abcdefabcdee")
    job.status = "done"
    job.created_at = time.time() - 999_999
    _jobs[job.id] = job

    with patch("app.pipeline.collect.JOB_TTL_SECONDS", 60):
        sweep_old_jobs(tmp_path)

    assert not d.exists()
    assert job.id not in _jobs


def test_keeps_recent_terminal_job(tmp_path: Path):
    d = _mkdir(tmp_path, "abcdefabcded")
    job = Job(id="abcdefabcded")
    job.status = "done"
    job.created_at = time.time()  # fresh
    _jobs[job.id] = job

    with patch("app.pipeline.collect.JOB_TTL_SECONDS", 60):
        sweep_old_jobs(tmp_path)

    assert d.is_dir()
    assert job.id in _jobs


def test_orphan_dir_falls_back_to_mtime(tmp_path: Path):
    """Directories with no registry entry (e.g. left over from a prior
    server run) still get swept by mtime."""
    d = _mkdir(tmp_path, "abcdefabcdec")
    # Backdate the directory.
    old = time.time() - 999_999
    import os

    os.utime(d, (old, old))

    with patch("app.pipeline.collect.JOB_TTL_SECONDS", 60):
        sweep_old_jobs(tmp_path)

    assert not d.exists()


def test_to_state_includes_created_at():
    """Job.to_state() must persist created_at so it survives a server restart."""
    ts = 12345.0
    job = Job(id="x", created_at=ts)
    assert "created_at" in job.to_state()
    assert job.to_state()["created_at"] == ts


def test_load_all_jobs_restores_created_at(tmp_path: Path):
    """load_all_jobs must restore created_at from metadata, not reset to time.time()."""
    job_dir = tmp_path / "restorejob1"
    job_dir.mkdir()
    old_ts = 1_000.0
    (job_dir / "metadata.json").write_text(
        json.dumps({"job_id": "restorejob1", "status": "done", "created_at": old_ts})
    )
    _jobs.clear()
    with patch("app.core.persistence.JOBS_DIR", tmp_path):
        load_all_jobs()
    job = _jobs.get("restorejob1")
    assert job is not None, "job was not restored"
    assert job.created_at == old_ts, f"expected {old_ts}, got {job.created_at}"


def test_restored_job_swept_after_ttl(tmp_path: Path):
    """Regression: a job restored via load_all_jobs with an old created_at
    must be swept by sweep_old_jobs — the TTL clock must not be reset on restart."""
    job_dir = tmp_path / "oldrestorejob"
    job_dir.mkdir()
    (job_dir / "stems").mkdir()
    old_ts = 1_000.0  # ancient — well past any TTL
    (job_dir / "metadata.json").write_text(
        json.dumps({"job_id": "oldrestorejob", "status": "done", "created_at": old_ts})
    )
    _jobs.clear()
    with patch("app.core.persistence.JOBS_DIR", tmp_path):
        load_all_jobs()
    assert "oldrestorejob" in _jobs, "job was not restored into registry"

    with patch("app.pipeline.collect.JOB_TTL_SECONDS", 60):
        sweep_old_jobs(tmp_path)

    assert not job_dir.exists(), "restored job directory was not swept despite old created_at"
    assert "oldrestorejob" not in _jobs


def test_orphan_stat_filenotfound_silenced(tmp_path: Path):
    """FileNotFoundError from d.stat() in the orphan branch must not propagate.

    Regression for the concurrent-sweep race: two sweep threads can iterate
    the same orphan directory. Thread A deletes it; Thread B has already passed
    is_dir() (True) and then calls stat() on the now-deleted path, raising
    FileNotFoundError. That error must be swallowed so the new job does not
    spuriously transition to 'error'.
    """
    d = _mkdir(tmp_path, "orphan_ghost_race")
    # No registry entry -> orphan elif branch in sweep_old_jobs

    real_stat = type(d).stat
    stat_call_counts: dict[str, int] = {}

    def counting_stat(self, **kwargs):
        key = str(self)
        n = stat_call_counts.get(key, 0) + 1
        stat_call_counts[key] = n
        # is_dir() calls stat() first (n==1); the explicit elif d.stat() is n==2.
        # Raise on the second call to simulate the concurrent deletion race.
        if self == d and n >= 2:
            raise FileNotFoundError(
                f"[Errno 2] simulated concurrent deletion: {self}"
            )
        return real_stat(self, **kwargs)

    with patch.object(type(d), "stat", counting_stat):
        with patch("app.pipeline.collect.JOB_TTL_SECONDS", 60):
            sweep_old_jobs(tmp_path)  # must not raise FileNotFoundError


def test_skip_terminal_job_with_active_readers(tmp_path: Path):
    """sweep_old_jobs must not delete a terminal, expired job directory while
    it has active HTTP readers (e.g. a stem is currently being streamed)."""
    d = _mkdir(tmp_path, "abcdefabcdeb")
    job = Job(id="abcdefabcdeb")
    job.status = "done"
    job.created_at = time.time() - 999_999  # ancient — well past any TTL
    _jobs[job.id] = job

    inc_readers(job.id)
    try:
        with patch("app.pipeline.collect.JOB_TTL_SECONDS", 60):
            sweep_old_jobs(tmp_path)
    finally:
        dec_readers(job.id)

    assert d.is_dir(), "directory must not be deleted while a reader is active"
    assert job.id in _jobs, "registry entry must not be removed while a reader is active"
