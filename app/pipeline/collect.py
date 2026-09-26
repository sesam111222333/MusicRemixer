from __future__ import annotations

import shutil
import time
from pathlib import Path

from app.core.config import JOB_TTL_SECONDS, STEMS
from app.core.registry import all_jobs as registry_all
from app.core.registry import claim_for_sweep, release_sweep_claim
from app.core.registry import remove as registry_remove

_TERMINAL = frozenset(("done", "error", "cancelled"))


def collect(stems_root: Path, job_dir: Path) -> list[str]:
    """Move separated stems (job_dir/_bsr_stems/) into the job's stems/ dir
    and remove the intermediate directory."""
    target_dir = job_dir / "stems"
    target_dir.mkdir(exist_ok=True)
    found: list[str] = []
    for name in STEMS:
        src = stems_root / f"{name}.wav"
        if src.exists():
            shutil.move(str(src), target_dir / f"{name}.wav")
            found.append(name)
    shutil.rmtree(stems_root, ignore_errors=True)
    if not found:
        raise RuntimeError("no stems produced by the separator")
    return found


def cleanup_source(job_dir: Path) -> None:
    """Delete the source audio file after collect. The source is 100-300 MB,
    so getting rid of it is the bulk of disk reclaim per job; only the
    stems remain."""
    for f in job_dir.glob("source.*"):
        f.unlink(missing_ok=True)


def sweep_old_jobs(jobs_dir: Path) -> None:
    """Delete job directories older than JOB_TTL_SECONDS and remove them from
    the in-memory registry. Called once per new job submission so stale data
    doesn't accumulate on disk.

    Prefers Job.created_at over directory mtime (which can be touched by
    unrelated filesystem events), and never deletes the directory of an
    active (non-terminal) registered job even if its timestamp looks old.
    Falls back to mtime for orphan directories left over from a previous
    server run, since the registry is in-memory only."""
    cutoff = time.time() - JOB_TTL_SECONDS
    if not jobs_dir.is_dir():
        return
    jobs = registry_all()
    for d in jobs_dir.iterdir():
        if not d.is_dir():
            continue
        job = jobs.get(d.name)
        if job is not None:
            if job.status not in _TERMINAL:
                continue  # never delete an active job's working dir
            if job.created_at >= cutoff:
                continue
        else:
            try:
                if d.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue  # deleted by a concurrent sweep, or unreadable (ESTALE, EPERM)
        if not claim_for_sweep(d.name):
            continue  # stem files are actively being streamed; defer deletion
        try:
            shutil.rmtree(d, ignore_errors=True)
            registry_remove(d.name)
        finally:
            release_sweep_claim(d.name)

    # Second pass: purge registry entries for terminal jobs whose directories
    # were already deleted (e.g. error jobs cleaned up by the runner). These
    # are never visited by the directory loop above, so without this pass they
    # would accumulate in _jobs for the lifetime of the server process.
    for job_id, job in jobs.items():
        if job.status not in _TERMINAL:
            continue
        if job.created_at >= cutoff:
            continue
        if (jobs_dir / job_id).is_dir():
            continue  # has a directory; the first pass handled (or deferred) it
        registry_remove(job_id)
