from __future__ import annotations

import io
import math
import subprocess
import zipfile

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.core.config import JOB_ID_RE, JOBS_DIR, STEM_NAMES
from app.core.registry import get as registry_get

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
    path = _resolve_stem_path(job_id, name)
    return Response(
        status_code=200,
        headers={
            "content-type": "audio/wav",
            "content-length": str(path.stat().st_size),
            "accept-ranges": "bytes",
            "content-disposition": f'inline; filename="{name}.wav"',
        },
    )


@router.get("/jobs/{job_id}/stems/{name}.wav")
def get_stem(job_id: str, name: str) -> FileResponse:
    path = _resolve_stem_path(job_id, name)
    return FileResponse(
        path,
        media_type="audio/wav",
        headers={"Content-Disposition": f'inline; filename="{name}.wav"'},
    )


@router.get("/jobs/{job_id}/stems.zip")
def download_all_stems(job_id: str) -> StreamingResponse:
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    stems_dir = (JOBS_DIR / job_id / "stems").resolve()
    if not stems_dir.is_dir() or not stems_dir.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="stems not found")
    wav_files = sorted(stems_dir.glob("*.wav"))
    if not wav_files:
        raise HTTPException(status_code=404, detail="no stems found")

    def generate():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for f in wav_files:
                zf.write(f, f.name)
        buf.seek(0)
        while chunk := buf.read(65536):
            yield chunk

    safe = (job.title or job_id).replace("/", "_").replace("\\", "_")[:80]
    filename = f"{safe}_stems.zip"
    return StreamingResponse(
        generate(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_STEMS_SAMPLE_RATE = 44100  # Demucs/BSRoFormer output sample rate


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
    vol_values = [v.strip() for v in volumes.split(",") if v.strip()]
    pitch_values = [p.strip() for p in pitches.split(",") if p.strip()]

    if not stem_names:
        raise HTTPException(status_code=422, detail="no stems specified")

    # Pad missing volumes and pitches with defaults
    while len(vol_values) < len(stem_names):
        vol_values.append("1.0")
    while len(pitch_values) < len(stem_names):
        pitch_values.append("0")

    stems_dir = (JOBS_DIR / job_id / "stems").resolve()
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
        try:
            pitch = max(-12, min(12, round(float(pitch_str))))
        except ValueError:
            pitch = 0
        triples.append((name, vol, pitch))

    if not triples:
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
    cmd += ["-filter_complex", filter_complex, "-map", "[out]", "-f", "wav", "-"]

    # Run ffmpeg fully before committing to HTTP 200 so any failure becomes a
    # proper 500 instead of a silently corrupt/truncated WAV.
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="mix timed out")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"ffmpeg unavailable: {exc}")

    if result.returncode != 0:
        stderr_text = result.stderr.decode(errors="replace").strip()
        raise HTTPException(status_code=500, detail=f"mix failed: {stderr_text or 'ffmpeg exited with non-zero status'}")

    wav_bytes = result.stdout

    def generate():
        offset = 0
        while offset < len(wav_bytes):
            yield wav_bytes[offset : offset + 65536]
            offset += 65536

    safe = (job.title or job_id).replace("/", "_").replace("\\", "_")[:80]
    has_pitch = any(p != 0 for _, _, p in triples)
    suffix = "_pitched" if has_pitch else ""
    return StreamingResponse(
        generate(),
        media_type="audio/wav",
        headers={"Content-Disposition": f'attachment; filename="{safe}_mix{suffix}.wav"'},
    )
