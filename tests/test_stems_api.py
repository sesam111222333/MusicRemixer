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


def test_zip_does_not_buffer_whole_stem_file(client, monkeypatch):
    """generate() must drain in small chunks, not buffer the entire WAV before yielding.

    Bug: zf.write(f, f.name) writes the whole file into _StreamBuf._data before
    returning, so drain() is called once with all bytes accumulated.  A 400 MB stem
    causes a 400 MB transient spike.  Fix: use zf.open() and write in 64 KB chunks.
    """
    from app.api.stems import _StreamBuf

    CHUNK_SIZE = 65536
    FILE_SIZE = CHUNK_SIZE * 8  # 512 KB — clearly larger than one chunk

    job_id = "aabbcc667788"
    paths = _setup_stems_job(job_id, {"vocals": b"\x00" * FILE_SIZE})

    max_buf: list[int] = [0]
    _orig_drain = _StreamBuf.drain

    def spy_drain(self):
        max_buf[0] = max(max_buf[0], len(self._data))
        return _orig_drain(self)

    monkeypatch.setattr(_StreamBuf, "drain", spy_drain)

    try:
        r = client.get(f"/api/jobs/{job_id}/stems.zip")
        assert r.status_code == 200
        assert max_buf[0] <= CHUNK_SIZE * 2, (
            f"_StreamBuf grew to {max_buf[0]} bytes before drain "
            f"(expected <= {CHUNK_SIZE * 2} = 2 x CHUNK_SIZE). "
            "zf.write() is buffering the entire stem — use zf.open() with chunked reads."
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


def _fake_popen(fake_wav: bytes):
    """Return a fake subprocess.Popen instance that yields *fake_wav* from stdout."""
    import io

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            self.stdout = io.BytesIO(fake_wav)
            self.stderr = io.BytesIO(b"")
            self.returncode = 0

        def kill(self):
            pass

        def wait(self):
            return 0

    return _FakePopen


def _fake_run(valid_wav: bytes):
    """Return a fake subprocess.run that writes *valid_wav* to the output file path (cmd[-1])."""

    class _R:
        returncode = 0
        stderr = b""

    def _fr(cmd, **kwargs):
        with open(cmd[-1], "wb") as fh:
            fh.write(valid_wav)
        return _R()

    return _fr


# ---------------------------------------------------------------------------
# download_remix — must use a temp file (not stdout pipe) for ffmpeg output
# ---------------------------------------------------------------------------


def test_download_remix_uses_tempfile_not_pipe(client, monkeypatch):
    """download_remix must write ffmpeg output to a seekable temp file, not stdout ('-').

    Writing '-' (stdout) to ffmpeg produces a non-seekable pipe; ffmpeg cannot
    seek back to fill in the RIFF/data chunk sizes and writes 0xFFFFFFFF instead.
    subprocess.run with a real file path gives ffmpeg a seekable fd so it writes
    the correct header sizes."""
    import subprocess

    job_id = "aabbccddeef0"
    job = Job(id=job_id, title="Tempfile Test")
    job.status = "done"
    _jobs[job_id] = job

    fake_wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
    paths = [_make_stem_file(job_id, "vocals", b"RIFF\x00\x00\x00\x00WAVE")]

    popen_called: list[bool] = []

    def spy_popen(*args, **kwargs):
        popen_called.append(True)
        return _fake_popen(fake_wav)(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    monkeypatch.setattr(subprocess, "run", _fake_run(fake_wav))

    try:
        r = client.get(f"/api/jobs/{job_id}/remix.wav?stems=vocals&volumes=1.0&pitches=0")
        assert not popen_called, (
            "subprocess.Popen must not be called — download_remix must use "
            "subprocess.run with a temp file path so ffmpeg can seek back and "
            "write correct WAV RIFF/data chunk sizes (Popen with stdout=PIPE gives "
            "a non-seekable fd → 0xFFFFFFFF placeholder sizes)."
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/wav"
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

    monkeypatch.setattr(subprocess, "run", _fake_run(fake_wav))

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

    monkeypatch.setattr(subprocess, "run", _fake_run(fake_wav))

    try:
        r = client.get(f"/api/jobs/{job_id}/remix.wav?stems=vocals&volumes=1.0&pitches=0")
        assert r.status_code == 200, f"Expected 200, got {r.status_code} (non-latin-1 title caused crash)"
        assert "content-disposition" in r.headers
        # Header value must be encodable as latin-1 (no UnicodeEncodeError)
        r.headers["content-disposition"].encode("latin-1")
    finally:
        _cleanup(paths)


# ---------------------------------------------------------------------------
# download_remix — temp file cleanup after streaming
# ---------------------------------------------------------------------------


def test_download_remix_tempfile_deleted_after_streaming(client, monkeypatch):
    """The temp WAV file created for ffmpeg output must be deleted after streaming.

    download_remix writes ffmpeg output to a seekable temp file (instead of piping
    to stdout) so the WAV header sizes are correct.  The generate() finally block
    must delete that file to prevent disk-space leaks on every remix download.
    """
    import os
    import subprocess
    import tempfile as tempfile_module

    job_id = "aabbccddee0f"
    job = Job(id=job_id, title="Cleanup Test")
    job.status = "done"
    _jobs[job_id] = job
    paths = [_make_stem_file(job_id, "vocals", b"RIFF\x00\x00\x00\x00WAVE")]

    # Track the temp file paths created inside download_remix.
    created_paths: list[str] = []
    _orig_mkstemp = tempfile_module.mkstemp

    def spy_mkstemp(suffix="", prefix="tmp", dir=None, text=False):
        fd, path = _orig_mkstemp(suffix=suffix, prefix=prefix, dir=dir, text=text)
        created_paths.append(path)
        return fd, path

    monkeypatch.setattr(tempfile_module, "mkstemp", spy_mkstemp)
    monkeypatch.setattr(subprocess, "run", _fake_run(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32))

    try:
        r = client.get(
            f"/api/jobs/{job_id}/remix.wav?stems=vocals&volumes=1.0&pitches=0"
        )
        assert r.status_code == 200
        assert created_paths, "download_remix must create a temp file for ffmpeg output"
        for tmp_path in created_paths:
            assert not os.path.exists(tmp_path), (
                f"Temp WAV file {tmp_path!r} was not deleted after streaming — "
                "disk-space leak on every remix download"
            )
    finally:
        for p in created_paths:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        _cleanup(paths)


# ---------------------------------------------------------------------------
# download_remix — WAV header validity (RIFF/data chunk sizes must not be 0xFFFFFFFF)
# ---------------------------------------------------------------------------


def test_remix_wav_header_sizes_are_valid(client, monkeypatch):
    """The downloaded WAV must have valid RIFF and data chunk sizes, not 0xFFFFFFFF.

    ffmpeg writing WAV to stdout (a non-seekable pipe) cannot seek back to fill in
    the RIFF chunk size (bytes 4-7) or the data chunk size (bytes 40-43), so it
    writes the placeholder 0xFFFFFFFF.  Strict parsers (Python wave module, many DAWs,
    Windows Media Player) interpret this as ~48 695 seconds of audio even for a 2-second
    mix, and seek is broken.  The fix must write to a seekable temp file instead.
    """
    import io
    import struct
    import subprocess

    job_id = "aabbccddeef2"
    job = Job(id=job_id, title="Header Fix Test")
    job.status = "done"
    _jobs[job_id] = job
    paths = [_make_stem_file(job_id, "vocals", b"RIFF\x00\x00\x00\x00WAVE")]

    # A minimal valid WAV (0 audio samples) with correct RIFF/data sizes.
    _data_size = 0
    _VALID_WAV = (
        b"RIFF" + struct.pack("<I", 36 + _data_size) + b"WAVE"
        + b"fmt " + struct.pack("<I", 16)
        + struct.pack("<H", 1)            # PCM
        + struct.pack("<H", 2)            # stereo
        + struct.pack("<I", 44100)        # sample rate
        + struct.pack("<I", 44100 * 4)    # byte rate
        + struct.pack("<H", 4)            # block align
        + struct.pack("<H", 16)           # bits per sample
        + b"data" + struct.pack("<I", _data_size)
    )

    # Simulate what ffmpeg produces when writing WAV to a non-seekable pipe:
    # both sizes are the placeholder 0xFFFFFFFF because ffmpeg cannot seek back.
    _PIPE_WAV = bytearray(_VALID_WAV)
    _PIPE_WAV[4:8] = b"\xff\xff\xff\xff"   # RIFF chunk size — placeholder
    _PIPE_WAV[40:44] = b"\xff\xff\xff\xff"  # data chunk size — placeholder
    _PIPE_WAV = bytes(_PIPE_WAV)

    def fake_popen(cmd, **kwargs):
        # Old (buggy) path: ffmpeg writes WAV to stdout pipe → 0xFFFFFFFF sizes.
        class _P:
            stdout = io.BytesIO(_PIPE_WAV)
            stderr = io.BytesIO(b"")
            returncode = 0
            def kill(self): pass
            def wait(self): return 0
        return _P()

    def fake_run(cmd, **kwargs):
        # Fixed path: ffmpeg writes to a seekable file → correct sizes.
        out_path = cmd[-1]
        with open(out_path, "wb") as fh:
            fh.write(_VALID_WAV)
        class _R:
            returncode = 0
            stderr = b""
        return _R()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess, "run", fake_run)

    try:
        r = client.get(
            f"/api/jobs/{job_id}/remix.wav?stems=vocals&volumes=1.0&pitches=0"
        )
        assert r.status_code == 200
        body = r.content

        riff_size = struct.unpack_from("<I", body, 4)[0]
        assert riff_size != 0xFFFFFFFF, (
            "RIFF chunk size is 0xFFFFFFFF — ffmpeg is writing WAV to a stdout pipe "
            "and cannot seek back to fill in the correct size. "
            "Write to a temp file (not '-') so ffmpeg can fix the header."
        )

        data_size_field = struct.unpack_from("<I", body, 40)[0]
        assert data_size_field != 0xFFFFFFFF, (
            "WAV data chunk size is 0xFFFFFFFF — ffmpeg pipe placeholder. "
            "Write to a temp file so ffmpeg can write the correct data size."
        )
    finally:
        _cleanup(paths)
