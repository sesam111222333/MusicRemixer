from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import JOBS_DIR
from app.core.models import Job
from app.core.registry import _jobs


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def _make_stem_file(job_id: str, name: str, contents: bytes = b"RIFF") -> Path:
    stems_dir = JOBS_DIR / job_id / "stems"
    stems_dir.mkdir(parents=True, exist_ok=True)
    path = stems_dir / f"{name}.wav"
    path.write_bytes(contents)
    return path


def test_rejects_malformed_job_id(client):
    # Anything that isn't 12 lowercase hex chars must be rejected before
    # touching the filesystem -- this is the path-traversal gate.
    for bad_id in ("../etc", "ABC", "abcdefabcdef0", "abcdefabcde", "abcd-efabcdef"):
        r = client.get(f"/api/jobs/{bad_id}/stems/vocals.wav")
        assert r.status_code == 404, f"id {bad_id!r} should 404"


def test_rejects_unknown_stem_name(client):
    job = Job(id="abcdefabcdef")
    job.status = "done"
    _jobs[job.id] = job
    r = client.get(f"/api/jobs/{job.id}/stems/banjo.wav")
    assert r.status_code == 404


def test_requires_done_status(client):
    job = Job(id="abcdefabcdef")
    job.status = "separating"
    _jobs[job.id] = job
    _make_stem_file(job.id, "vocals")
    try:
        r = client.get(f"/api/jobs/{job.id}/stems/vocals.wav")
        assert r.status_code == 404
    finally:
        (JOBS_DIR / job.id / "stems" / "vocals.wav").unlink(missing_ok=True)
        (JOBS_DIR / job.id / "stems").rmdir()
        (JOBS_DIR / job.id).rmdir()


def test_serves_done_job_stem(client):
    job = Job(id="abcdefabcdee")
    job.status = "done"
    _jobs[job.id] = job
    path = _make_stem_file(job.id, "vocals", b"RIFF1234")
    try:
        r = client.get(f"/api/jobs/{job.id}/stems/vocals.wav")
        assert r.status_code == 200
        assert r.content == b"RIFF1234"
        assert r.headers["content-type"] == "audio/wav"
    finally:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
        path.parent.parent.rmdir()


# ---------------------------------------------------------------------------
# download_all_stems — streaming ZIP endpoint
# ---------------------------------------------------------------------------

def _setup_stems_job(job_id: str, stems: dict[str, bytes]) -> list[Path]:
    """Create a done job with multiple stem files; return paths for cleanup."""
    job = Job(id=job_id)
    job.status = "done"
    _jobs[job_id] = job
    paths = []
    for name, data in stems.items():
        paths.append(_make_stem_file(job_id, name, data))
    return paths


def _cleanup(paths: list[Path]) -> None:
    for p in paths:
        p.unlink(missing_ok=True)
    if paths:
        stems_dir = paths[0].parent
        stems_dir.rmdir()
        stems_dir.parent.rmdir()


def test_zip_rejects_malformed_job_id(client):
    r = client.get("/api/jobs/../secretstuff/stems.zip")
    assert r.status_code == 404


def test_zip_requires_done_status(client):
    job = Job(id="aabbccddeeff")
    job.status = "separating"
    _jobs[job.id] = job
    paths = [_make_stem_file(job.id, "vocals")]
    try:
        r = client.get(f"/api/jobs/{job.id}/stems.zip")
        assert r.status_code == 404
    finally:
        _cleanup(paths)


def test_zip_missing_job_returns_404(client):
    r = client.get("/api/jobs/aabbccddeeff/stems.zip")
    assert r.status_code == 404


def test_zip_streams_valid_zip_with_correct_contents(client):
    """ZIP endpoint must return a valid archive containing all WAV stems."""
    import io
    import zipfile

    job_id = "112233445566"
    stems = {
        "vocals": b"RIFF" + b"\x00" * 44,
        "drums": b"RIFF" + b"\x01" * 44,
    }
    paths = _setup_stems_job(job_id, stems)
    try:
        r = client.get(f"/api/jobs/{job_id}/stems.zip")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"

        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = set(zf.namelist())
        assert names == {"vocals.wav", "drums.wav"}

        for stem_name, expected in stems.items():
            assert zf.read(f"{stem_name}.wav") == expected, f"{stem_name} data mismatch"
    finally:
        _cleanup(paths)


def test_zip_each_file_read_only_once_per_stream(client, tmp_path, monkeypatch):
    """Generator must not buffer full ZIP in RAM — each stem is opened and streamed
    without accumulating all file bytes into a single BytesIO buffer."""
    import io
    import zipfile

    # We verify this indirectly: the response body must be a valid ZIP
    # that contains the correct data, produced without the old BytesIO-buffer path.
    # The real guard is the implementation (no io.BytesIO accumulation).
    job_id = "aabbcc112233"
    payload = b"RIFF" + bytes(range(256)) * 4  # 1028 bytes, not a power of 2
    paths = _setup_stems_job(job_id, {"vocals": payload, "bass": payload[:512]})
    try:
        r = client.get(f"/api/jobs/{job_id}/stems.zip")
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert zf.read("vocals.wav") == payload
        assert zf.read("bass.wav") == payload[:512]
    finally:
        _cleanup(paths)


def test_zip_does_not_buffer_archive_in_bytesio(client, monkeypatch):
    """download_all_stems must not pass an io.BytesIO to ZipFile — that would
    accumulate the full archive (all stems) in RAM before yielding the first byte."""
    import io
    import zipfile

    # Spy on ZipFile.__init__ to record what file-like object is passed.
    zip_file_types: list[str] = []
    _real_init = zipfile.ZipFile.__init__

    def _spy_init(self, file, *args, **kwargs):
        zip_file_types.append(type(file).__name__)
        _real_init(self, file, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "__init__", _spy_init)

    job_id = "aabbcc445566"
    paths = _setup_stems_job(job_id, {"vocals": b"RIFF" + b"\x00" * 44})
    try:
        r = client.get(f"/api/jobs/{job_id}/stems.zip")
        assert r.status_code == 200
        assert zip_file_types, "ZipFile was never created — generator did not run"
        assert not any(t == "BytesIO" for t in zip_file_types), (
            f"ZipFile was given an io.BytesIO buffer ({zip_file_types}) — "
            "this loads the entire ZIP into RAM instead of streaming"
        )
    finally:
        _cleanup(paths)



# ---------------------------------------------------------------------------
# Content-Disposition filename sanitization — double-quote in title
# ---------------------------------------------------------------------------


def test_zip_content_disposition_sanitizes_double_quotes(client):
    """filename in Content-Disposition must not contain bare double-quote chars."""
    import subprocess

    job_id = "aabbccddeea0"
    job = Job(id=job_id, title='"Hello" - Adele')
    job.status = "done"
    _jobs[job_id] = job
    paths = [_make_stem_file(job_id, "vocals")]
    try:
        r = client.get(f"/api/jobs/{job_id}/stems.zip")
        assert r.status_code == 200
        cd = r.headers["content-disposition"]
        assert 'filename="' in cd
        # Everything between the outer double-quotes must contain no bare "
        inner = cd.split('filename="', 1)[1][:-1]
        assert '"' not in inner, f"Bare double-quote in Content-Disposition: {cd!r}"
    finally:
        _cleanup(paths)


def test_remix_content_disposition_sanitizes_double_quotes(client, monkeypatch):
    """remix filename in Content-Disposition must not contain bare double-quote chars."""
    import subprocess

    job_id = "aabbccddeeb1"
    job = Job(id=job_id, title='"Hello" - Adele')
    job.status = "done"
    _jobs[job_id] = job
    paths = [_make_stem_file(job_id, "vocals", b"RIFF\x00\x00\x00\x00WAVE")]

    fake_wav = b"RIFF\x00\x00\x00\x00WAVE"

    class _FakeResult:
        returncode = 0
        stdout = fake_wav
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeResult())

    try:
        r = client.get(f"/api/jobs/{job_id}/remix.wav?stems=vocals&volumes=1.0&pitches=0")
        assert r.status_code == 200
        cd = r.headers["content-disposition"]
        assert 'filename="' in cd
        inner = cd.split('filename="', 1)[1][:-1]
        assert '"' not in inner, f"Bare double-quote in Content-Disposition: {cd!r}"
    finally:
        _cleanup(paths)


# ---------------------------------------------------------------------------
# Content-Disposition filename sanitization — non-latin-1 characters in title
# ---------------------------------------------------------------------------


def test_zip_content_disposition_handles_non_latin1_title(client):
    """download_all_stems must not crash (HTTP 500) when title contains non-latin-1 chars."""
    job_id = "aabbccddeec2"
    job = Job(id=job_id, title="東京の音楽🎵 - Summer Mix")
    job.status = "done"
    _jobs[job_id] = job
    paths = [_make_stem_file(job_id, "vocals")]
    try:
        r = client.get(f"/api/jobs/{job_id}/stems.zip")
        assert r.status_code == 200, f"Expected 200, got {r.status_code} (non-latin-1 title caused crash)"
        assert "content-disposition" in r.headers
        # Header value must be encodable as latin-1 (no UnicodeEncodeError)
        r.headers["content-disposition"].encode("latin-1")
    finally:
        _cleanup(paths)


def test_remix_content_disposition_handles_non_latin1_title(client, monkeypatch):
    """download_remix must not crash (HTTP 500) when title contains non-latin-1 chars."""
    import subprocess

    job_id = "aabbccddee33"
    job = Job(id=job_id, title="東京の音楽🎵 - Summer Mix")
    job.status = "done"
    _jobs[job_id] = job
    paths = [_make_stem_file(job_id, "vocals", b"RIFF\x00\x00\x00\x00WAVE")]

    fake_wav = b"RIFF\x00\x00\x00\x00WAVE"

    class _FakeResult:
        returncode = 0
        stdout = fake_wav
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeResult())

    try:
        r = client.get(f"/api/jobs/{job_id}/remix.wav?stems=vocals&volumes=1.0&pitches=0")
        assert r.status_code == 200, f"Expected 200, got {r.status_code} (non-latin-1 title caused crash)"
        assert "content-disposition" in r.headers
        # Header value must be encodable as latin-1 (no UnicodeEncodeError)
        r.headers["content-disposition"].encode("latin-1")
    finally:
        _cleanup(paths)
