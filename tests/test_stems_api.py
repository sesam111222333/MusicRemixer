from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import JOBS_DIR
from app.core.models import Job
from app.core.registry import _jobs, _readers


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


def test_zip_excludes_original_and_mix(client):
    """download_all_stems must not include original.wav or mix.wav in the ZIP.

    The pipeline writes original.wav (re-encoded source when a subset of stems is
    selected) and mix.wav (amix of selected stems) into the stems directory, but
    these are derived/combined tracks, not isolated demucs stems.  Including them
    gives the user misleading audio and a larger-than-expected download.
    """
    import io
    import zipfile

    job_id = "aabbccddeef4"
    stems = {
        "vocals": b"RIFF" + b"\x00" * 44,
        "drums": b"RIFF" + b"\x01" * 44,
        "original": b"RIFF" + b"\x02" * 44,
        "mix": b"RIFF" + b"\x03" * 44,
    }
    paths = _setup_stems_job(job_id, stems)
    try:
        r = client.get(f"/api/jobs/{job_id}/stems.zip")
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = set(zf.namelist())
        assert "original.wav" not in names, (
            "download_all_stems must not include original.wav — it is a re-encoded "
            "source track produced by the pipeline, not an isolated demucs stem"
        )
        assert "mix.wav" not in names, (
            "download_all_stems must not include mix.wav — it is an amix of the "
            "selected stems produced by the pipeline, not an isolated demucs stem"
        )
        assert "vocals.wav" in names, "vocals.wav (a real stem) must be in the ZIP"
        assert "drums.wav" in names, "drums.wav (a real stem) must be in the ZIP"
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


def _fake_popen_write_file(fake_wav: bytes):
    """Return a fake Popen class that writes *fake_wav* to cmd[-1] (temp output path)
    and supports communicate(timeout=...) — matches the Popen+set_proc pattern."""

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            with open(cmd[-1], "wb") as fh:
                fh.write(fake_wav)
            self.returncode = 0
            self._stderr = b""

        def communicate(self, timeout=None):
            return b"", self._stderr

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
    """download_remix must write ffmpeg output to a seekable temp file, not stdout pipe.

    When ffmpeg writes WAV to a non-seekable stdout pipe it cannot seek back to
    fill in the RIFF/data chunk sizes and writes 0xFFFFFFFF instead.  The correct
    fix is to pass the temp file path as the output argument in cmd so ffmpeg
    has a seekable fd.  Using Popen with stdout=PIPE reproduces the old bug even
    with a temp-file path — stdout must be DEVNULL (or None) so the output file
    argument in cmd is the only place ffmpeg writes."""
    import subprocess

    job_id = "aabbccddeef0"
    job = Job(id=job_id, title="Tempfile Test")
    job.status = "done"
    _jobs[job_id] = job

    fake_wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
    paths = [_make_stem_file(job_id, "vocals", b"RIFF\x00\x00\x00\x00WAVE")]

    stdout_kwargs: list = []

    def spy_popen(cmd, **kwargs):
        stdout_kwargs.append(kwargs.get("stdout"))
        return _fake_popen_write_file(fake_wav)(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy_popen)

    try:
        r = client.get(f"/api/jobs/{job_id}/remix.wav?stems=vocals&volumes=1.0&pitches=0")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/wav"
        assert stdout_kwargs, "subprocess.Popen was never called"
        for stdout in stdout_kwargs:
            assert stdout != subprocess.PIPE, (
                "download_remix passes stdout=PIPE to Popen — ffmpeg cannot seek "
                "back and writes 0xFFFFFFFF placeholder sizes in the WAV header. "
                "Use stdout=DEVNULL and write output to the temp file path in cmd."
            )
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

    monkeypatch.setattr(subprocess, "Popen", _fake_popen_write_file(fake_wav))

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

    monkeypatch.setattr(subprocess, "Popen", _fake_popen_write_file(fake_wav))

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
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_write_file(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32))

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


def test_remix_empty_volume_slot_uses_default_not_shifts(client, monkeypatch):
    """Empty slot in volumes/pitches must be treated as default, not compacted.

    GET remix.wav?stems=vocals,drums,bass&volumes=0.5,,0.8 should map:
      vocals → 0.5, drums → 1.0 (default), bass → 0.8

    Bug: filtering out empty elements with `if v.strip()` compacts the list to
    ["0.5", "0.8"], then padding appends "1.0" at the end, giving
      vocals → 0.5, drums → 0.8, bass → 1.0  ← shifted by one.
    """
    import subprocess

    job_id = "aabbccddeea1"
    job = Job(id=job_id, title="Empty Slot Test")
    job.status = "done"
    _jobs[job_id] = job
    paths = [
        _make_stem_file(job_id, "vocals", b"RIFF\x00\x00\x00\x00WAVE"),
        _make_stem_file(job_id, "drums", b"RIFF\x00\x00\x00\x00WAVE"),
        _make_stem_file(job_id, "bass", b"RIFF\x00\x00\x00\x00WAVE"),
    ]

    captured_cmd: list[list[str]] = []

    class _SpyPopen:
        def __init__(self, cmd, **kwargs):
            captured_cmd.append(list(cmd))
            with open(cmd[-1], "wb") as fh:
                fh.write(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32)
            self.returncode = 0
            self._stderr = b""

        def communicate(self, timeout=None):
            return b"", self._stderr

        def kill(self):
            pass

        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", _SpyPopen)

    try:
        r = client.get(
            f"/api/jobs/{job_id}/remix.wav?stems=vocals,drums,bass&volumes=0.5,,0.8&pitches=,,3"
        )
        assert r.status_code == 200
        assert captured_cmd, "subprocess.Popen was never called"

        cmd = captured_cmd[0]
        # Find the -filter_complex argument
        fc_idx = cmd.index("-filter_complex")
        filter_complex = cmd[fc_idx + 1]

        # vocals (index 0): volume must be 0.5
        assert "[0]volume=0.500000" in filter_complex, (
            f"vocals must have volume=0.5; got filter_complex={filter_complex!r}"
        )
        # drums (index 1): empty volume slot → default 1.0
        assert "[1]volume=1.000000" in filter_complex, (
            f"drums must have volume=1.0 (default for empty slot); "
            f"got filter_complex={filter_complex!r}"
        )
        # bass (index 2): volume must be 0.8
        assert "[2]volume=0.800000" in filter_complex, (
            f"bass must have volume=0.8; got filter_complex={filter_complex!r}"
        )

        # pitches: vocals=0 (empty→default), drums=0 (empty→default), bass=3
        # When pitch==0 the filter looks like [N]volume=...[aN] with no asetrate.
        # When pitch==3 it contains asetrate.
        assert "asetrate" not in filter_complex.split("[2]")[0].rsplit("[1]", 1)[-1], (
            f"drums must have no pitch shift; got filter_complex={filter_complex!r}"
        )
        assert "asetrate" in filter_complex.split("[2]")[1], (
            f"bass must have pitch shift (semitone 3); got filter_complex={filter_complex!r}"
        )
    finally:
        _cleanup(paths)


def test_remix_wav_header_sizes_are_valid(client, monkeypatch):
    """The downloaded WAV must have valid RIFF and data chunk sizes, not 0xFFFFFFFF.

    ffmpeg writing WAV to stdout (a non-seekable pipe) cannot seek back to fill in
    the RIFF chunk size (bytes 4-7) or the data chunk size (bytes 40-43), so it
    writes the placeholder 0xFFFFFFFF.  Strict parsers (Python wave module, many DAWs,
    Windows Media Player) interpret this as ~48 695 seconds of audio even for a 2-second
    mix, and seek is broken.  The fix must write to a seekable temp file instead.
    """
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

    def fake_popen(cmd, **kwargs):
        # download_remix uses Popen with stdout=DEVNULL and writes output to cmd[-1].
        # A seekable file lets ffmpeg fill in the correct RIFF/data chunk sizes.
        out_path = cmd[-1]
        with open(out_path, "wb") as fh:
            fh.write(_VALID_WAV)
        class _P:
            returncode = 0
            def communicate(self, timeout=None): return b"", b""
            def kill(self): pass
            def wait(self): return 0
        return _P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

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


# ---------------------------------------------------------------------------
# Race condition: sweep_old_jobs must not delete a job directory between
# path.is_file() and inc_readers in get_stem.
# ---------------------------------------------------------------------------

def test_get_stem_not_500_when_sweep_races(monkeypatch):
    """get_stem must not return 500 if sweep_old_jobs runs after is_file()
    returns True but before inc_readers is called.

    Regression: inc_readers was called AFTER _resolve_stem_path, leaving a window
    where sweep could delete the directory between the is_file() check and
    inc_readers.  Fix: inc_readers must be called BEFORE _resolve_stem_path so
    sweep sees has_readers=True and defers deletion.
    """
    import shutil
    from unittest.mock import patch

    from app.api import stems as stems_module
    from app.pipeline.collect import sweep_old_jobs

    job_id = "aabbccddff01"
    job = Job(id=job_id)
    job.status = "done"
    job.created_at = 0.0  # ancient — would be swept under any TTL
    _jobs[job_id] = job

    path = _make_stem_file(job_id, "vocals", b"RIFF" + b"\x00" * 44)
    job_dir = path.parent.parent

    _orig_resolve = stems_module._resolve_stem_path

    def _resolve_then_sweep(jid, name):
        result = _orig_resolve(jid, name)
        # Simulate sweep racing after is_file() passes inside _resolve_stem_path.
        # With fix (inc_readers before _resolve_stem_path): sweep sees
        # has_readers=True → skips deletion → FileResponse 200.
        # Without fix (inc_readers after _resolve_stem_path): sweep sees
        # has_readers=False → deletes directory → FileResponse raises → 500.
        with patch("app.pipeline.collect.JOB_TTL_SECONDS", 1):
            sweep_old_jobs(JOBS_DIR)
        return result

    monkeypatch.setattr(stems_module, "_resolve_stem_path", _resolve_then_sweep)

    from app.main import app as main_app
    tc = TestClient(main_app, raise_server_exceptions=False)
    try:
        r = tc.get(f"/api/jobs/{job_id}/stems/vocals.wav")
        assert r.status_code == 200, (
            f"Expected 200 but got {r.status_code}: "
            "inc_readers was called after _resolve_stem_path, so sweep could delete "
            "the job directory between path.is_file() and inc_readers, causing a 500."
        )
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        _readers.pop(job_id, None)
        _jobs.pop(job_id, None)


def test_head_stem_not_500_when_sweep_races(monkeypatch):
    """head_stem must not return 500 if sweep_old_jobs deletes the job directory
    between _resolve_stem_path (path.is_file() check) and path.stat().

    Bug: head_stem never calls inc_readers, so sweep can delete the directory in
    that window and path.stat() raises FileNotFoundError → unhandled → 500.
    Fix: call inc_readers before _resolve_stem_path (as get_stem does) so sweep
    sees has_readers=True and defers deletion while the HEAD request is in flight.
    """
    import shutil
    from unittest.mock import patch

    from app.api import stems as stems_module
    from app.pipeline.collect import sweep_old_jobs

    job_id = "aabbccddff02"
    job = Job(id=job_id)
    job.status = "done"
    job.created_at = 0.0  # ancient — swept under any TTL
    _jobs[job_id] = job

    path = _make_stem_file(job_id, "vocals", b"RIFF" + b"\x00" * 44)
    job_dir = path.parent.parent

    _orig_resolve = stems_module._resolve_stem_path

    def _resolve_then_sweep(jid, name):
        result = _orig_resolve(jid, name)
        # Sweep races between path.is_file() (in _resolve_stem_path) and path.stat()
        # (in head_stem). With fix (inc_readers before _resolve_stem_path): sweep
        # sees has_readers=True → defers deletion → head_stem returns 200.
        # Without fix (no inc_readers): sweep deletes directory → path.stat() raises
        # FileNotFoundError → 500.
        with patch("app.pipeline.collect.JOB_TTL_SECONDS", 1):
            sweep_old_jobs(JOBS_DIR)
        return result

    monkeypatch.setattr(stems_module, "_resolve_stem_path", _resolve_then_sweep)

    from app.main import app as main_app
    tc = TestClient(main_app, raise_server_exceptions=False)
    try:
        r = tc.head(f"/api/jobs/{job_id}/stems/vocals.wav")
        assert r.status_code == 200, (
            f"head_stem returned {r.status_code} instead of 200: sweep deleted the "
            "job directory between _resolve_stem_path and path.stat(). "
            "Call inc_readers before _resolve_stem_path to block sweep."
        )
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        _readers.pop(job_id, None)
        _jobs.pop(job_id, None)


# ---------------------------------------------------------------------------
# Reader-count leak on aborted download (client disconnect mid-stream)
# ---------------------------------------------------------------------------


async def test_get_stem_dec_readers_on_aborted_download():
    """_readers must reach zero even when the client disconnects mid-stream.

    Bug: get_stem registered dec_readers via background_tasks.add_task.
    Starlette only runs BackgroundTasks after the HTTP send loop completes
    normally — if send() raises on disconnect, the task never executes and
    _readers[job_id] stays > 0 forever, blocking sweep_old_jobs forever.
    Fix: use StreamingResponse with a try/finally generator, mirroring the
    zip and remix endpoints.
    """
    import inspect

    from fastapi import BackgroundTasks
    from fastapi.responses import StreamingResponse

    from app.api import stems as stems_mod

    job_id = "aabbccddee88"
    job = Job(id=job_id)
    job.status = "done"
    _jobs[job_id] = job
    path = _make_stem_file(job_id, "vocals", b"RIFF" + b"\x00" * 100_000)

    try:
        # Support both old signature (background_tasks kwarg) and the fixed one.
        sig = inspect.signature(stems_mod.get_stem)
        call_kwargs = {}
        if "background_tasks" in sig.parameters:
            call_kwargs["background_tasks"] = BackgroundTasks()

        response = stems_mod.get_stem(job_id, "vocals", **call_kwargs)

        assert isinstance(response, StreamingResponse), (
            f"get_stem returned {type(response).__name__!r}, expected StreamingResponse. "
            "FileResponse defers dec_readers to a BackgroundTask that never runs on "
            "client disconnect — fix: use StreamingResponse with try/finally generator."
        )

        # Simulate a client disconnect by closing the async body iterator early.
        # Starlette wraps the sync generator in an async generator via
        # iterate_in_threadpool, which calls iterator.close() on GeneratorExit,
        # propagating it into the sync generator's finally block.
        gen = response.body_iterator
        await gen.__anext__()  # start streaming (first chunk emitted)
        await gen.aclose()     # disconnect → GeneratorExit → finally: dec_readers

        assert _readers.get(job_id, 0) == 0, (
            f"_readers[{job_id!r}] = {_readers.get(job_id, 0)} after simulated abort; "
            "expected 0. dec_readers must live in the generator try/finally block, "
            "not in a BackgroundTask."
        )
    finally:
        _readers.pop(job_id, None)
        path.unlink(missing_ok=True)
        path.parent.rmdir()
        path.parent.parent.rmdir()


# ---------------------------------------------------------------------------
# Reader-count leak when path.stat() raises after inc_readers succeeds
# ---------------------------------------------------------------------------


def test_get_stem_dec_readers_when_stat_raises(client, monkeypatch):
    """get_stem must not leak reader count when path.stat() raises OSError.

    Bug: size = path.stat().st_size lies outside the try/except that calls
    dec_readers on HTTPException.  If stat() raises (e.g. transient network
    file error or concurrent deletion), inc_readers is never balanced by
    dec_readers — _readers[job_id] stays > 0 permanently, claim_for_sweep
    always returns False, and sweep_old_jobs can never delete the job directory.
    Fix: wrap path.stat() so that any OSError also calls dec_readers before raising.
    """
    import pathlib

    from app.api import stems as stems_mod

    job_id = "aabbccddeedd"
    job = Job(id=job_id)
    job.status = "done"
    _jobs[job_id] = job
    path = _make_stem_file(job_id, "vocals", b"RIFF" + b"\x00" * 44)

    # Bypass _resolve_stem_path so its internal path.is_file() → stat() call
    # does not interfere with our patch.  Only the explicit path.stat().st_size
    # on line ~75 of get_stem will hit the patched version.
    monkeypatch.setattr(stems_mod, "_resolve_stem_path", lambda jid, name: path)

    _orig_stat = pathlib.Path.stat

    def _stat_raises(self, *args, **kwargs):
        if self == path:
            raise OSError("simulated transient stat failure")
        return _orig_stat(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", _stat_raises)

    try:
        r = client.get(f"/api/jobs/{job_id}/stems/vocals.wav")
        assert r.status_code == 404, (
            f"Expected 404 when path.stat() raises OSError, got {r.status_code}. "
            "The server should return 404 (stem not found), not 500."
        )
        assert _readers.get(job_id, 0) == 0, (
            f"_readers[{job_id!r}] = {_readers.get(job_id, 0)} after stat() failure; "
            "expected 0. inc_readers was not balanced by dec_readers — reader count "
            "leaks and claim_for_sweep will never be able to delete the job directory."
        )
    finally:
        _readers.pop(job_id, None)
        path.unlink(missing_ok=True)
        path.parent.rmdir()
        path.parent.parent.rmdir()


# ---------------------------------------------------------------------------
# Reader-count leak when mkstemp fails after inc_readers
# ---------------------------------------------------------------------------


def test_download_remix_dec_readers_when_mkstemp_fails(client, monkeypatch):
    """download_remix must not leak reader count when mkstemp raises OSError.

    Bug: inc_readers is called before tempfile.mkstemp, but mkstemp is outside
    the try block, so if it raises (e.g. full disk, no temp inodes) dec_readers
    is never called — _readers[job_id] stays > 0 permanently and sweep_old_jobs
    can never delete the job directory, which worsens a full-disk situation.
    Fix: wrap mkstemp inside the try so OSError triggers dec_readers.
    """
    import tempfile as tempfile_module

    job_id = "aabbccddeef3"
    job = Job(id=job_id, title="Mkstemp Fail Test")
    job.status = "done"
    _jobs[job_id] = job
    paths = [_make_stem_file(job_id, "vocals", b"RIFF\x00\x00\x00\x00WAVE")]

    def _mkstemp_raises(*args, **kwargs):
        raise OSError("simulated: no space left on device")

    monkeypatch.setattr(tempfile_module, "mkstemp", _mkstemp_raises)

    try:
        r = client.get(f"/api/jobs/{job_id}/remix.wav?stems=vocals&volumes=1.0&pitches=0")
        assert r.status_code == 500
        assert _readers.get(job_id, 0) == 0, (
            f"_readers[{job_id!r}] = {_readers.get(job_id, 0)} after mkstemp failure; "
            "expected 0. inc_readers must be balanced by dec_readers even when mkstemp raises."
        )
    finally:
        _readers.pop(job_id, None)
        _cleanup(paths)


# ---------------------------------------------------------------------------
# HTTP Range support in get_stem (RFC 7233)
# ---------------------------------------------------------------------------


def test_get_stem_range_returns_206_with_slice(client):
    """GET with Range: bytes=X-Y must return 206 with only the requested bytes.

    head_stem advertises accept-ranges: bytes, so browsers (and Safari in
    particular) send Range requests for media seeking.  get_stem must honour
    the Range header; returning 200 with the full file on a Range request
    breaks seeking and causes Safari to refuse playback entirely.
    """
    job_id = "aabbccddeefa"
    job = Job(id=job_id)
    job.status = "done"
    _jobs[job_id] = job
    payload = b"RIFF12345678"  # 12 bytes, easy to slice by hand
    path = _make_stem_file(job_id, "vocals", payload)
    try:
        r = client.get(
            f"/api/jobs/{job_id}/stems/vocals.wav",
            headers={"Range": "bytes=4-7"},
        )
        assert r.status_code == 206, (
            f"Expected 206 Partial Content for Range request, got {r.status_code}. "
            "get_stem ignores the Range header and returns 200 with the full file."
        )
        assert r.content == b"1234", (
            f"Expected bytes 4-7 = b'1234', got {r.content!r}"
        )
        assert "content-range" in r.headers, "206 response must include Content-Range"
        assert r.headers["content-range"] == f"bytes 4-7/{len(payload)}", (
            f"Wrong Content-Range: {r.headers.get('content-range')!r}"
        )
        assert r.headers.get("content-length") == "4", (
            f"Wrong Content-Length for partial response: {r.headers.get('content-length')!r}"
        )
    finally:
        _readers.pop(job_id, None)
        path.unlink(missing_ok=True)
        path.parent.rmdir()
        path.parent.parent.rmdir()


def test_get_stem_range_open_end_returns_rest_of_file(client):
    """Range: bytes=N- must return from byte N to end of file with 206."""
    job_id = "aabbccddeefb"
    job = Job(id=job_id)
    job.status = "done"
    _jobs[job_id] = job
    payload = b"RIFF12345678"
    path = _make_stem_file(job_id, "vocals", payload)
    try:
        r = client.get(
            f"/api/jobs/{job_id}/stems/vocals.wav",
            headers={"Range": "bytes=8-"},
        )
        assert r.status_code == 206
        assert r.content == b"5678"
        assert r.headers["content-range"] == f"bytes 8-11/{len(payload)}"
    finally:
        _readers.pop(job_id, None)
        path.unlink(missing_ok=True)
        path.parent.rmdir()
        path.parent.parent.rmdir()


def test_get_stem_no_range_still_returns_200(client):
    """A request without Range must still return 200 with full content."""
    job_id = "aabbccddeef9"
    job = Job(id=job_id)
    job.status = "done"
    _jobs[job_id] = job
    payload = b"RIFF12345678"
    path = _make_stem_file(job_id, "vocals", payload)
    try:
        r = client.get(f"/api/jobs/{job_id}/stems/vocals.wav")
        assert r.status_code == 200
        assert r.content == payload
    finally:
        _readers.pop(job_id, None)
        path.unlink(missing_ok=True)
        path.parent.rmdir()
        path.parent.parent.rmdir()


def test_get_stem_range_beyond_eof_returns_416(client):
    """GET with an unsatisfiable Range (start >= file size) must return 416 Range Not Satisfiable.

    RFC 7233 §4.4: the server MUST send 416 with Content-Range: bytes */<size> when
    the requested range start is at or beyond the end of the resource.
    Bug: _parse_range returns None for start >= total, which get_stem interprets as
    "no Range header" and falls through to return 200 with the full file.
    """
    job_id = "aabbccddeec3"
    job = Job(id=job_id)
    job.status = "done"
    _jobs[job_id] = job
    payload = b"RIFF12345678"  # 12 bytes
    path = _make_stem_file(job_id, "vocals", payload)
    try:
        r = client.get(
            f"/api/jobs/{job_id}/stems/vocals.wav",
            headers={"Range": "bytes=99999999-"},
        )
        assert r.status_code == 416, (
            f"Expected 416 Range Not Satisfiable for start beyond EOF, got {r.status_code}. "
            "_parse_range returns None for start >= total; get_stem then treats the "
            "request as if no Range was sent and returns 200 with the full file — "
            "RFC 7233 §4.4 violation."
        )
        assert "content-range" in r.headers, (
            "416 response must include Content-Range: bytes */<total>"
        )
        assert r.headers["content-range"] == f"bytes */{len(payload)}", (
            f"Expected Content-Range: bytes */12, got {r.headers.get('content-range')!r}"
        )
    finally:
        _readers.pop(job_id, None)
        path.unlink(missing_ok=True)
        path.parent.rmdir()
        path.parent.parent.rmdir()


# ---------------------------------------------------------------------------
# download_remix — set_proc registration and timeout
# ---------------------------------------------------------------------------


def test_download_remix_registers_proc_with_set_proc(client, monkeypatch):
    """download_remix must call set_proc during ffmpeg rendering so POST /cancel can abort it.

    Without set_proc registration the cancel API has no handle to call
    proc.terminate() on — a hung ffmpeg render cannot be interrupted from outside,
    binds a thread-pool worker indefinitely, and holds inc_readers so
    sweep_old_jobs and DELETE /jobs/{id} are blocked until the process exits.
    """
    import subprocess
    from app.api import stems as stems_module

    job_id = "aabbccddeec0"
    job = Job(id=job_id, title="Proc Reg Test")
    job.status = "done"
    _jobs[job_id] = job
    paths = [_make_stem_file(job_id, "vocals", b"RIFF\x00\x00\x00\x00WAVE")]

    set_proc_calls: list[tuple] = []

    def spy_set_proc(jid, proc):
        set_proc_calls.append((jid, proc))

    fake_wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
    # Provide fakes for both APIs so the test works regardless of which is used.
    monkeypatch.setattr(subprocess, "run", _fake_run(fake_wav))
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_write_file(fake_wav))
    # Patch set_proc in the stems module; raising=False so it works before
    # the import is added (the attribute is missing → test fails at assertion).
    monkeypatch.setattr(stems_module, "set_proc", spy_set_proc, raising=False)

    try:
        r = client.get(f"/api/jobs/{job_id}/remix.wav?stems=vocals&volumes=1.0&pitches=0")
        assert r.status_code == 200
        assert any(proc is not None for _, proc in set_proc_calls), (
            "set_proc was never called with a non-None proc — "
            "download_remix does not register the ffmpeg subprocess for cancellation."
        )
        assert any(proc is None for _, proc in set_proc_calls), (
            "set_proc(job_id, None) was never called — "
            "download_remix does not clear the proc registration after ffmpeg completes."
        )
    finally:
        _cleanup(paths)


def test_download_remix_ffmpeg_timeout_returns_500(client, monkeypatch):
    """download_remix must return 500 (not hang) when ffmpeg times out.

    Without a timeout on the ffmpeg call a stuck render blocks the thread-pool
    worker and holds inc_readers indefinitely. With communicate(timeout=300),
    TimeoutExpired must be caught and translated to an HTTP 500.
    """
    import subprocess

    job_id = "aabbccddeec1"
    job = Job(id=job_id)
    job.status = "done"
    _jobs[job_id] = job
    paths = [_make_stem_file(job_id, "vocals", b"RIFF\x00\x00\x00\x00WAVE")]

    fake_wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32

    class _TimeoutPopen:
        def __init__(self, cmd, **kwargs):
            self.returncode = -9
            self._cmd = cmd
            self._calls = 0

        def communicate(self, timeout=None):
            self._calls += 1
            if self._calls == 1:
                # First call (from communicate(timeout=300)) simulates a hang.
                raise subprocess.TimeoutExpired(cmd=self._cmd, timeout=timeout)
            # Second call (drain after kill) returns normally.
            return b"", b""

        def kill(self):
            pass

        def wait(self):
            return -9

    # Patch Popen to a process that always times out.
    # subprocess.run is NOT patched — if the code still uses run (no timeout),
    # it will try to invoke real ffmpeg on fake WAV files and return 500 from
    # ffmpeg exit code, not from timeout handling. That masks the bug.
    # We detect the fix by: Popen → TimeoutExpired → our handler → 500.
    # Current code (uses run, no timeout): run is not patched → real ffmpeg
    # fails on fake stems → 500 from ffmpeg error, not timeout → test would
    # still pass! So we need a different signal.
    # Solution: also patch subprocess.run to succeed (return 200); then:
    #   - current code (no timeout, uses run): run succeeds → 200 → FAIL
    #   - fixed code (uses Popen+timeout): Popen raises TimeoutExpired → 500 → PASS
    monkeypatch.setattr(subprocess, "run", _fake_run(fake_wav))
    monkeypatch.setattr(subprocess, "Popen", _TimeoutPopen)

    try:
        r = client.get(f"/api/jobs/{job_id}/remix.wav?stems=vocals&volumes=1.0&pitches=0")
        assert r.status_code == 500, (
            f"Expected 500 on ffmpeg timeout, got {r.status_code}. "
            "download_remix must use communicate(timeout=300) so a hung ffmpeg "
            "is killed and the request fails fast instead of blocking indefinitely."
        )
    finally:
        _cleanup(paths)
