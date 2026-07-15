from __future__ import annotations

import math
import os
import subprocess
import tempfile
import zipfile

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from app.core.config import JOB_ID_RE, JOBS_DIR, STEM_NAMES
from app.core.registry import dec_readers, get as registry_get, inc_readers

router = APIRouter(tags=["stems"])

# Stem files served by this endpoint: the 6 demucs stems + two
# pipeline-produced extras. "original" is the re-encoded source song
# (added when the user picked a strict subset), "mix" is the ffmpeg
# amix of the user's selected stems.
_ALLOWED_NAMES = frozenset(STEM_NAMES) | {"original", "mix"}


def _resolve_stem_path(job_id: str, name: str):
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if name not in _ALLOWED_NAMES:
        raise HTTPException(status_code=404, detail="unknown stem")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    path = (JOBS_DIR / job_id / "stems" / f"{name}.wav").resolve()
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="stem not found")
    return path


@router.head("/jobs/{job_id}/stems/{name}.wav")
def head_stem(job_id: str, name: str) -> Response:
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if not inc_readers(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    try:
        path = _resolve_stem_path(job_id, name)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="stem not found")
        return Response(
            status_code=200,
            headers={
                "content-type": "audio/wav",
                "content-length": str(size),
                "accept-ranges": "bytes",
                "content-disposition": f'inline; filename="{name}.wav"',
            },
        )
    finally:
        dec_readers(job_id)


@router.get("/jobs/{job_id}/stems/{name}.wav")
def get_stem(job_id: str, name: str) -> StreamingResponse:
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if not inc_readers(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    try:
        path = _resolve_stem_path(job_id, name)
        try:
            size = path.stat().st_size
        except OSError:
            raise HTTPException(status_code=404, detail="stem not found")
    except HTTPException:
        dec_readers(job_id)
        raise

    def generate():
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(65536):
                    yield chunk
        finally:
            dec_readers(job_id)

    return StreamingResponse(
        generate(),
        media_type="audio/wav",
        headers={
            "content-length": str(size),
            "content-disposition": f'inline; filename="{name}.wav"',
        },
    )


class _StreamBuf:
    """Non-seekable write buffer for streaming ZIP generation.

    zipfile requires tell() even on non-seekable streams (for computing local
    header offsets and the central directory offset), so we track total bytes
    written without buffering them after each drain().
    """

    __slots__ = ("_data", "_total")

    def __init__(self) -> None:
        self._data: bytearray = bytearray()
        self._total: int = 0

    def write(self, b: bytes) -> int:
        n = len(b)
        self._data.extend(b)
        self._total += n
        return n

    def flush(self) -> None:
        pass

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._total

    def drain(self) -> bytes:
        chunk = bytes(self._data)
        self._data.clear()
        return chunk


@router.get("/jobs/{job_id}/stems.zip")
def download_all_stems(job_id: str) -> StreamingResponse:
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    if not inc_readers(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    stems_dir = (JOBS_DIR / job_id / "stems").resolve()
    if not stems_dir.is_dir() or not stems_dir.is_relative_to(JOBS_DIR.resolve()):
        dec_readers(job_id)
        raise HTTPException(status_code=404, detail="stems not found")
    wav_files = sorted(stems_dir.glob("*.wav"))
    if not wav_files:
        dec_readers(job_id)
        raise HTTPException(status_code=404, detail="no stems found")

    def generate():
        try:
            buf = _StreamBuf()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
                for f in wav_files:
                    with zf.open(f.name, "w") as zentry:
                        with open(f, "rb") as src:
                            while chunk := src.read(65536):
                                zentry.write(chunk)
                                data = buf.drain()
                                if data:
                                    yield data
                    data = buf.drain()
                    if data:
                        yield data
            chunk = buf.drain()
            if chunk:
                yield chunk
        finally:
            dec_readers(job_id)

    safe = (job.title or job_id).replace("/", "_").replace("\\", "_").replace('"', "")[:80]
    safe = safe.encode("latin-1", errors="replace").decode("latin-1").replace("?", "_")
    filename = f"{safe}_stems.zip"
    return StreamingResponse(
        generate(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_STEMS_SAMPLE_RATE = 44100  # Demucs/BSRoFormer output sample rate


def _parse_pitch(s: str) -> int:
    """Parse a semitone offset from a query-param string.

    Falls back to 0 for any non-numeric input or for float infinity/NaN, which
    would make round() raise OverflowError — not caught by except ValueError.
    """
    try:
        return max(-12, min(12, round(float(s))))
    except (ValueError, OverflowError):
        return 0


@router.get("/jobs/{job_id}/remix.wav")
def download_remix(
    job_id: str,
    stems: str = Query(""),
    volumes: str = Query(""),
    pitches: str = Query(""),
) -> StreamingResponse:
    """Stream a custom mix of the given stems at the given volumes and pitch offsets via ffmpeg.

    pitches: comma-separated semitone integers in [-12, 12]. Uses asetrate+atempo to
    shift pitch without changing tempo. Missing values default to 0 (no shift).
    """
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")

    stem_names = [s.strip() for s in stems.split(",") if s.strip()]

    if not stem_names:
        raise HTTPException(status_code=422, detail="no stems specified")

    # Keep positional alignment: an empty slot (",,") means "use default",
    # not "compact the list". Filtering empties would shift all later values.
    _raw_vols = [v.strip() for v in volumes.split(",")] if volumes else []
    _raw_pitches = [p.strip() for p in pitches.split(",")] if pitches else []
    vol_values = [
        (_raw_vols[i] if i < len(_raw_vols) and _raw_vols[i] else "1.0")
        for i in range(len(stem_names))
    ]
    pitch_values = [
        (_raw_pitches[i] if i < len(_raw_pitches) and _raw_pitches[i] else "0")
        for i in range(len(stem_names))
    ]

    stems_dir = (JOBS_DIR / job_id / "stems").resolve()
    if not inc_readers(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    triples: list[tuple[str, float, int]] = []
    for name, vol_str, pitch_str in zip(stem_names, vol_values, pitch_values):
        if name not in _ALLOWED_NAMES:
            continue
        path = stems_dir / f"{name}.wav"
        if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
            continue
        try:
            vol = max(0.0, min(4.0, float(vol_str)))
        except ValueError:
            vol = 1.0
        pitch = _parse_pitch(pitch_str)
        triples.append((name, vol, pitch))

    if not triples:
        dec_readers(job_id)
        raise HTTPException(status_code=404, detail="no valid stems found")

    cmd: list[str] = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error"]
    for name, _, _ in triples:
        cmd += ["-i", str(stems_dir / f"{name}.wav")]

    filter_parts: list[str] = []
    for i, (_, v, p) in enumerate(triples):
        if p == 0:
            filter_parts.append(f"[{i}]volume={v:.6f}[a{i}]")
        else:
            # asetrate shifts pitch (and tempo); atempo corrects tempo back.
            # Range ±12 semitones keeps atempo within FFmpeg's [0.5, 2.0] limit.
            factor = 2 ** (p / 12)
            rate = _STEMS_SAMPLE_RATE * factor
            atempo = 1.0 / factor
            filter_parts.append(
                f"[{i}]volume={v:.6f},asetrate={rate:.4f},atempo={atempo:.8f}"
                f",aresample={_STEMS_SAMPLE_RATE}[a{i}]"
            )
    mixed_inputs = "".join(f"[a{i}]" for i in range(len(triples)))
    filter_complex = ";".join(filter_parts) + f";{mixed_inputs}amix=inputs={len(triples)}:normalize=0[out]"
    # Write to a temp file so ffmpeg can seek back and fill in the WAV RIFF/data
    # chunk sizes correctly.  Writing to stdout ('-') gives a non-seekable pipe,
    # which forces ffmpeg to write the placeholder 0xFFFFFFFF for both sizes.
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav")
    except OSError as exc:
        dec_readers(job_id)
        raise HTTPException(status_code=500, detail=f"could not create temp file: {exc}")
    os.close(tmp_fd)
    cmd += ["-filter_complex", filter_complex, "-map", "[out]", "-f", "wav", tmp_path]

    try:
        result = subprocess.run(cmd, stderr=subprocess.PIPE)
    except OSError as exc:
        os.unlink(tmp_path)
        dec_readers(job_id)
        raise HTTPException(status_code=500, detail=f"ffmpeg unavailable: {exc}")

    if result.returncode != 0:
        os.unlink(tmp_path)
        dec_readers(job_id)
        raise HTTPException(
            status_code=500,
            detail=f"ffmpeg: {result.stderr.decode(errors='replace')}",
        )

    def generate():
        try:
            with open(tmp_path, "rb") as fh:
                while chunk := fh.read(65536):
                    yield chunk
        finally:
            dec_readers(job_id)
            os.unlink(tmp_path)

    safe = (job.title or job_id).replace("/", "_").replace("\\", "_").replace('"', "")[:80]
    safe = safe.encode("latin-1", errors="replace").decode("latin-1").replace("?", "_")
    has_pitch = any(p != 0 for _, _, p in triples)
    suffix = "_pitched" if has_pitch else ""
    return StreamingResponse(
        generate(),
        media_type="audio/wav",
        headers={"Content-Disposition": f'attachment; filename="{safe}_mix{suffix}.wav"'},
    )
