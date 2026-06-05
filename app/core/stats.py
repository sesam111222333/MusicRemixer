from __future__ import annotations

import json
import time
from pathlib import Path

_STATS_FILE = Path(__file__).parent.parent / "data" / "job_stats.json"
_MAX_ENTRIES = 1000


def _load() -> list[dict]:
    try:
        return json.loads(_STATS_FILE.read_text())
    except Exception:
        return []


def _save(entries: list[dict]) -> None:
    _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries))
    tmp.replace(_STATS_FILE)


def record_completion(job_id: str, title: str | None, status: str) -> None:
    entries = _load()
    entries.append({
        "id": job_id,
        "displayName": title or job_id,
        "lastSeen": int(time.time() * 1000),
        "isOnline": False,
        "jobStatus": status,
    })
    _save(entries[-_MAX_ENTRIES:])


def get_stats_response() -> dict:
    from app.core.registry import all_jobs

    entries = _load()
    cutoff_24h = int((time.time() - 86400) * 1000)

    active = [
        {
            "id": jid,
            "displayName": j.title or jid,
            "lastSeen": int(time.time() * 1000),
            "isOnline": True,
        }
        for jid, j in all_jobs().items()
        if j.status not in ("done", "error", "cancelled")
    ]

    recent = list(reversed(entries[-50:]))

    return {
        "users": active + recent,
        "jobs_total": len(entries),
        "jobs_last_24h": sum(1 for e in entries if e.get("lastSeen", 0) > cutoff_24h),
    }
