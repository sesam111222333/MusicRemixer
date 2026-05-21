from __future__ import annotations

import json
import logging

from app.core.config import JOBS_DIR
from app.core.models import Job

logger = logging.getLogger("stemdeck.persistence")


def save_job(job: Job) -> None:
    """Write completed job metadata to disk so it survives server restarts."""
    job_dir = JOBS_DIR / job.id
    if not job_dir.is_dir():
        return
    try:
        meta_path = job_dir / "metadata.json"
        meta_path.write_text(json.dumps(job.to_state(), ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to save metadata for job %s: %s", job.id, exc)


def load_all_jobs() -> None:
    """Restore completed jobs from disk into the in-memory registry on startup."""
    from app.core.registry import register

    if not JOBS_DIR.is_dir():
        return
    loaded = 0
    for job_dir in JOBS_DIR.iterdir():
        if not job_dir.is_dir():
            continue
        meta_path = job_dir / "metadata.json"
        if not meta_path.is_file():
            continue
        try:
            state = json.loads(meta_path.read_text(encoding="utf-8"))
            if state.get("status") != "done":
                continue
            job = Job(
                id=state["job_id"],
                status="done",
                progress=1.0,
                stage_message="Done",
                title=state.get("title"),
                duration_sec=state.get("duration"),
                thumbnail=state.get("thumbnail"),
                bpm=state.get("bpm"),
                key=state.get("key"),
                scale=state.get("scale"),
                key_confidence=state.get("key_confidence"),
                lufs=state.get("lufs"),
                peak_db=state.get("peak_db"),
                stems=state.get("stems", []),
                backend=state.get("backend", "bsroformer"),
                selected_stems=state.get("selected_stems", []),
                mix_url=state.get("mix_url"),
            )
            register(job)
            loaded += 1
        except Exception as exc:
            logger.warning("Failed to restore job from %s: %s", job_dir.name, exc)
    if loaded:
        logger.info("Restored %d completed job(s) from disk", loaded)
