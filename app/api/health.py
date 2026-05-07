from __future__ import annotations

from fastapi import APIRouter

from app.core.registry import all_jobs

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    jobs = all_jobs()
    active = sum(1 for j in jobs.values() if j.status not in ("done", "error", "cancelled"))
    return {"status": "ok", "active_jobs": active}


@router.get("/stats")
def stats() -> dict:
    from app.core.stats import get_stats_response
    return get_stats_response()
