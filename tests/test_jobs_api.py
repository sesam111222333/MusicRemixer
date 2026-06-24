from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.registry import _jobs


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each test gets a fresh in-memory registry."""
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client():
    # Patch run_pipeline so the test never spawns Demucs / yt-dlp.
    async def _noop_pipeline(job, url, jobs_dir):
        return None

    with patch("app.api.jobs.run_pipeline", _noop_pipeline):
        from app.main import app

        with TestClient(app) as c:
            yield c


@pytest.fixture
def client_upload(tmp_path):
    async def _noop_pipeline(job, source_path, jobs_dir):
        return None

    with (
        patch("app.api.jobs.run_pipeline_from_file", _noop_pipeline),
        patch("app.api.jobs.JOBS_DIR", tmp_path),
    ):
        from app.main import app

        with TestClient(app) as c:
            yield c


def test_post_rejects_invalid_url(client):
    r = client.post("/api/jobs", json={"url": "https://example.com/foo"})
    assert r.status_code == 422
    assert "unsupported host" in r.json()["detail"]


def test_post_rejects_empty_url(client):
    r = client.post("/api/jobs", json={"url": ""})
    assert r.status_code == 422


def test_post_accepts_youtube_url(client):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 200
    assert "job_id" in r.json()
    assert len(r.json()["job_id"]) == 12


def test_get_unknown_job_returns_404(client):
    r = client.get("/api/jobs/000000000000")
    assert r.status_code == 404


def test_cancel_unknown_job_returns_404(client):
    r = client.post("/api/jobs/000000000000/cancel")
    assert r.status_code == 404


def test_delete_running_job_rejected(client):
    # Submit a job; the patched pipeline is a noop so status stays "queued"
    # for the test's lifetime (no event loop tick advances it).
    r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    job_id = r.json()["job_id"]
    # Simulate a still-running job by leaving it on its default status.
    r = client.delete(f"/api/jobs/{job_id}")
    assert r.status_code == 409


def test_cancel_sets_flag_and_returns_state(client):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    job_id = r.json()["job_id"]
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200
    assert _jobs[job_id].cancel_requested is True


def test_cancel_after_done_is_idempotent(client):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    job_id = r.json()["job_id"]
    _jobs[job_id].status = "done"
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200
    assert _jobs[job_id].cancel_requested is False  # not flipped on terminal jobs


def test_upload_rejects_oversized_file(client_upload):
    """A payload exceeding MAX_UPLOAD_BYTES must return 413 without writing the
    entire body to disk."""
    oversized = b"\x00" * 200  # 200 bytes; limit will be patched to 100
    with patch("app.api.jobs.MAX_UPLOAD_BYTES", 100):
        r = client_upload.post(
            "/api/jobs/upload",
            files={"file": ("test.mp3", oversized, "audio/mpeg")},
        )
    assert r.status_code == 413


def test_url_job_limit_enforced(client):
    """After MAX_PENDING_JOBS pending jobs, POST /api/jobs must return 429."""
    with patch("app.api.jobs.MAX_PENDING_JOBS", 2, create=True):
        url = "https://youtu.be/dQw4w9WgXcQ"
        for _ in range(2):
            r = client.post("/api/jobs", json={"url": url})
            assert r.status_code == 200
        r = client.post("/api/jobs", json={"url": url})
        assert r.status_code == 429


def test_upload_job_limit_enforced(client_upload):
    """After MAX_PENDING_JOBS pending jobs, POST /api/jobs/upload must return 429
    without writing any upload bytes to disk."""
    audio = b"fake_audio_data"
    with patch("app.api.jobs.MAX_PENDING_JOBS", 2, create=True):
        for _ in range(2):
            r = client_upload.post(
                "/api/jobs/upload",
                files={"file": ("test.mp3", audio, "audio/mpeg")},
            )
            assert r.status_code == 200
        r = client_upload.post(
            "/api/jobs/upload",
            files={"file": ("test.mp3", audio, "audio/mpeg")},
        )
        assert r.status_code == 429


def test_terminal_jobs_dont_count_toward_limit(client):
    """Finished jobs (done/error/cancelled) must not occupy a pending slot."""
    url = "https://youtu.be/dQw4w9WgXcQ"
    with patch("app.api.jobs.MAX_PENDING_JOBS", 1, create=True):
        r = client.post("/api/jobs", json={"url": url})
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        # Limit reached — second request while first is still pending → 429
        r = client.post("/api/jobs", json={"url": url})
        assert r.status_code == 429

        # Slot freed once first job completes
        _jobs[job_id].status = "done"
        r = client.post("/api/jobs", json={"url": url})
        assert r.status_code == 200


def test_upload_client_disconnect_cleans_up(tmp_path):
    """ClientDisconnect raised during file.read must remove the job from the
    registry and delete the job_dir so no orphan accumulates."""
    from starlette.datastructures import UploadFile
    from starlette.requests import ClientDisconnect

    async def _noop_pipeline(job, source_path, jobs_dir):
        return None

    async def _disconnect(*args, **kwargs):
        raise ClientDisconnect()

    with (
        patch("app.api.jobs.run_pipeline_from_file", _noop_pipeline),
        patch("app.api.jobs.JOBS_DIR", tmp_path),
        patch.object(UploadFile, "read", _disconnect),
    ):
        from app.main import app

        with TestClient(app, raise_server_exceptions=False) as c:
            c.post(
                "/api/jobs/upload",
                files={"file": ("test.mp3", b"audio_data", "audio/mpeg")},
            )

    assert not _jobs, "job must be removed from registry on ClientDisconnect"
    assert not any(tmp_path.iterdir()), "job_dir must be deleted on ClientDisconnect"
