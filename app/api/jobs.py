from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.core.config import DEFAULT_BACKEND, JOBS_DIR, MAX_UPLOAD_BYTES, STEMS_4, STEMS_6
from app.core.models import Job
from app.core.registry import get as registry_get
from app.core.registry import get_proc as registry_get_proc
from app.core.registry import register as registry_register
from app.core.registry import remove as registry_remove
from app.pipeline import run_pipeline
from app.pipeline.download import InvalidYouTubeURL, validate_youtube_url
from app.pipeline.runner import run_pipeline_from_file

router = APIRouter(tags=["jobs"])

_ALLOWED_EXTS = frozenset(
    (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".opus", ".webm", ".wma")
)


_VALID_BACKENDS = frozenset(("demucs", "bsroformer"))


def _resolve_backend(raw: str | None) -> str:
    if raw and raw in _VALID_BACKENDS:
        return raw
    return DEFAULT_BACKEND


class JobRequest(BaseModel):
    url: str
    stems: list[str] | None = None
    backend: str | None = None


@router.post("")
async def create_job(payload: JobRequest) -> dict[str, str]:
    try:
        url = validate_youtube_url(payload.url)
    except InvalidYouTubeURL as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    backend = _resolve_backend(payload.backend)
    stem_names = STEMS_4 if backend == "bsroformer" else STEMS_6
    selected = [s for s in payload.stems if s in stem_names] if payload.stems else list(stem_names)
    if not selected:
        selected = list(stem_names)
    job = registry_register(Job(id=uuid.uuid4().hex[:12], backend=backend, selected_stems=selected))
    asyncio.create_task(run_pipeline(job, url, JOBS_DIR))
    return {"job_id": job.id}


@router.post("/upload")
async def create_job_from_upload(
    file: UploadFile = File(...),
    stems: str | None = Form(None),
    backend: str | None = Form(None),
) -> dict[str, str]:
    resolved_backend = _resolve_backend(backend)
    stem_names = STEMS_4 if resolved_backend == "bsroformer" else STEMS_6
    selected = list(stem_names)
    if stems:
        try:
            parsed = json.loads(stems)
            if isinstance(parsed, list):
                selected = [s for s in parsed if s in stem_names] or list(stem_names)
        except (json.JSONDecodeError, TypeError):
            pass

    ext = Path(file.filename or "upload").suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(status_code=422, detail=f"Unsupported file type: {ext or '(none)'}")

    job = registry_register(Job(id=uuid.uuid4().hex[:12], backend=resolved_backend, selected_stems=selected))
    job.title = Path(file.filename or "upload").stem

    job_dir = JOBS_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    source_path = job_dir / f"source{ext}"

    try:
        bytes_written = 0
        exceeded = False
        with source_path.open("wb") as out:
            while chunk := await file.read(8 * 1024 * 1024):
                bytes_written += len(chunk)
                if bytes_written > MAX_UPLOAD_BYTES:
                    exceeded = True
                    break
                out.write(chunk)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        registry_remove(job.id)
        raise

    if exceeded:
        shutil.rmtree(job_dir, ignore_errors=True)
        registry_remove(job.id)
        raise HTTPException(status_code=413, detail="Upload exceeds maximum allowed size")

    asyncio.create_task(run_pipeline_from_file(job, source_path, JOBS_DIR))
    return {"job_id": job.id}


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_state()


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in ("done", "error", "cancelled"):
        return job.to_state()
    job.cancel_requested = True
    proc = registry_get_proc(job_id)
    if proc is not None and proc.poll() is None:
        proc.terminate()
    return job.to_state()


@router.delete("/{job_id}")
def delete_job(job_id: str) -> dict[str, str]:
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("done", "error", "cancelled"):
        raise HTTPException(status_code=409, detail="job is still running")
    job_dir = JOBS_DIR / job_id
    if job_dir.is_dir():
        shutil.rmtree(job_dir, ignore_errors=True)
    registry_remove(job_id)
    return {"job_id": job_id, "status": "deleted"}
