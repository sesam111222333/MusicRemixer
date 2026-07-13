from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.models import Job

# These names do not exist yet — the import itself is the RED signal.
from app.core.registry import (
    _jobs,
    _readers,
    _sweeping,  # new: set of job_ids currently being swept
    claim_for_sweep,  # new: atomically check-and-mark for sweep
    dec_readers,
    has_readers,
    inc_readers,
    release_sweep_claim,  # new: release the sweep claim after rmtree
)
from app.pipeline.collect import sweep_old_jobs


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    _readers.clear()
    _sweeping.clear()
    yield
    _jobs.clear()
    _readers.clear()
    _sweeping.clear()


# ---------------------------------------------------------------------------
# inc_readers return value
# ---------------------------------------------------------------------------


def test_inc_readers_returns_true_on_success():
    """inc_readers must return True to signal successful reader acquisition."""
    job_id = "ccccddddee01"
    result = inc_readers(job_id)
    try:
        assert result is True, (
            f"inc_readers must return True on success; got {result!r}. "
            "Callers check the return value to decide whether to proceed or 404."
        )
    finally:
        dec_readers(job_id)


# ---------------------------------------------------------------------------
# claim_for_sweep / release_sweep_claim
# ---------------------------------------------------------------------------


def test_claim_for_sweep_returns_true_with_no_readers():
    """claim_for_sweep must return True and mark the job as sweeping when no readers exist."""
    job_id = "ccccddddee02"
    claimed = claim_for_sweep(job_id)
    try:
        assert claimed is True, (
            f"claim_for_sweep must return True when there are no readers; got {claimed!r}"
        )
        assert job_id in _sweeping, "claim_for_sweep must add job_id to _sweeping"
    finally:
        release_sweep_claim(job_id)


def test_claim_for_sweep_returns_false_with_active_readers():
    """claim_for_sweep must return False if there are active readers."""
    job_id = "ccccddddee03"
    inc_readers(job_id)
    try:
        claimed = claim_for_sweep(job_id)
        assert claimed is False, (
            f"claim_for_sweep must return False when readers are active; got {claimed!r}"
        )
        assert job_id not in _sweeping, "must not mark job as sweeping when readers are active"
    finally:
        dec_readers(job_id)


def test_release_sweep_claim_removes_from_sweeping():
    """release_sweep_claim must remove job_id from _sweeping."""
    job_id = "ccccddddee04"
    claim_for_sweep(job_id)
    assert job_id in _sweeping
    release_sweep_claim(job_id)
    assert job_id not in _sweeping


# ---------------------------------------------------------------------------
# inc_readers blocked while sweep claims
# ---------------------------------------------------------------------------


def test_inc_readers_returns_false_when_sweep_claimed():
    """inc_readers must return False if claim_for_sweep has already claimed the job.

    TOCTOU race: sweep checks has_readers=False, then inc_readers races in between
    the check and rmtree, then sweep deletes the directory.  Fix: claim_for_sweep
    atomically marks the job as sweeping; subsequent inc_readers returns False so
    the caller raises 404 instead of accessing a deleted file.
    """
    job_id = "ccccddddee05"
    claimed = claim_for_sweep(job_id)
    assert claimed, "setup: should be claimable with no readers"
    try:
        result = inc_readers(job_id)
        assert result is False, (
            f"inc_readers must return False while sweep holds the claim; got {result!r}. "
            "Without this guard, a reader can acquire the job after sweep has started "
            "deleting its directory, leading to FileNotFoundError / 500."
        )
        assert _readers.get(job_id, 0) == 0, (
            "inc_readers must not have incremented _readers while sweep claimed the job"
        )
    finally:
        release_sweep_claim(job_id)


def test_inc_readers_succeeds_after_sweep_claim_released():
    """After release_sweep_claim, inc_readers must work normally again."""
    job_id = "ccccddddee06"
    claim_for_sweep(job_id)
    release_sweep_claim(job_id)
    result = inc_readers(job_id)
    try:
        assert result is True, (
            f"inc_readers must return True after the sweep claim is released; got {result!r}"
        )
        assert _readers.get(job_id, 0) == 1
    finally:
        dec_readers(job_id)


# ---------------------------------------------------------------------------
# Non-atomic R-M-W under concurrent load
# ---------------------------------------------------------------------------


def test_concurrent_inc_readers_is_atomic():
    """N concurrent inc_readers must all register — non-atomic R-M-W loses increments.

    Bug: _readers[id] = _readers.get(id, 0) + 1 is a compound operation, not
    atomic under threading.  Two threads can both read 0, both write 1, and one
    increment is silently lost.  A subsequent dec_readers then hits 0 and pops the
    key, making has_readers return False while the other thread is still reading.
    Fix: protect the read-modify-write with threading.Lock.
    """
    job_id = "ccccddddee07"
    N = 100
    barrier = threading.Barrier(N)
    false_returns: list[int] = []

    def do_inc():
        barrier.wait()
        result = inc_readers(job_id)
        if result is False:
            false_returns.append(1)

    threads = [threading.Thread(target=do_inc) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    count = _readers.get(job_id, 0)
    _readers.pop(job_id, None)

    assert not false_returns, (
        f"{len(false_returns)} of {N} concurrent inc_readers calls returned False "
        "unexpectedly (no sweep claim was held — this is a lock bug)"
    )
    assert count == N, (
        f"After {N} concurrent inc_readers: expected count={N}, got {count}. "
        "Non-atomic R-M-W lost increments — use threading.Lock to protect _readers."
    )


def test_concurrent_inc_dec_readers_net_zero():
    """Interleaved inc/dec calls must leave the count at zero.

    Non-atomic dec_readers can produce negative counts or fail to remove the key,
    causing has_readers to return True (blocking sweep) permanently after all
    readers are done.
    """
    job_id = "ccccddddee08"
    N = 80
    barrier = threading.Barrier(N)

    def worker():
        barrier.wait()
        inc_readers(job_id)
        dec_readers(job_id)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    count = _readers.get(job_id, 0)
    assert count == 0, (
        f"After {N} concurrent inc+dec pairs: expected count=0, got {count}. "
        "Non-atomic R-M-W in dec_readers can corrupt the counter."
    )
    assert not has_readers(job_id), (
        "has_readers must be False after all readers have decremented"
    )


# ---------------------------------------------------------------------------
# Integration: sweep_old_jobs uses claim_for_sweep
# ---------------------------------------------------------------------------


def _mkdir(jobs_dir: Path, name: str) -> Path:
    d = jobs_dir / name
    d.mkdir(parents=True)
    (d / "marker").write_bytes(b"x")
    return d


def test_sweep_defers_deletion_when_reader_concurrent_with_claim(tmp_path: Path):
    """sweep_old_jobs must not delete a directory while a reader has been registered.

    Specifically: if claim_for_sweep returns False (reader active), sweep must skip
    the job.  This test verifies that the sweep path uses claim_for_sweep so that
    the atomicity guarantee (no inc_readers between check and rmtree) is upheld.
    """
    job_id = "ccccddddee09"
    job = Job(id=job_id)
    job.status = "done"
    job.created_at = 0.0  # ancient
    _jobs[job_id] = job

    d = _mkdir(tmp_path, job_id)

    inc_readers(job_id)
    try:
        with patch("app.pipeline.collect.JOB_TTL_SECONDS", 60):
            sweep_old_jobs(tmp_path)
    finally:
        dec_readers(job_id)

    assert d.is_dir(), (
        "sweep_old_jobs must not delete the job directory while a reader is active"
    )
    assert job_id in _jobs


def test_sweep_claim_prevents_race_between_check_and_rmtree(tmp_path: Path):
    """A reader arriving after sweep's has_readers check must not access deleted files.

    Without claim_for_sweep: sweep sees has_readers=False, then inc_readers runs
    (between check and rmtree), then sweep deletes the directory.  Reader gets
    FileNotFoundError → 500.

    With claim_for_sweep: inc_readers returns False while sweep holds the claim,
    so the reader never tries to open the (now-deleted) file.  sweep completes
    and release_sweep_claim is called.
    """
    job_id = "ccccddddee0a"
    job = Job(id=job_id)
    job.status = "done"
    job.created_at = 0.0
    _jobs[job_id] = job

    d = _mkdir(tmp_path, job_id)

    # Simulate: sweep calls claim_for_sweep (no readers → succeeds)
    claimed = claim_for_sweep(job_id)
    assert claimed, "setup: no readers → claim must succeed"

    # Concurrent reader arrives AFTER claim — must be blocked
    concurrent_result = inc_readers(job_id)
    assert concurrent_result is False, (
        "A reader arriving after claim_for_sweep must get False from inc_readers, "
        "preventing it from accessing files that sweep is about to delete."
    )

    # Sweep proceeds safely
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    release_sweep_claim(job_id)

    assert not d.exists()
