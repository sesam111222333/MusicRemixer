from __future__ import annotations

import json
import time
from pathlib import Path

_STATS_FILE = Path(__file__).parent.parent / "data" / "job_stats.json"
_MAX_ENTRIES = 1000


def _load() -> tuple[list[dict], int]:
    """Return (recent_entries, cumulative_total).

    Handles old list-format files (total = len(list)) transparently.
    """
    try:
        raw = json.loads(_STATS_FILE.read_text())
        if isinstance(raw, list):
            return raw, len(raw)
        return raw["entries"], raw["total"]
    except Exception:
        return [], 0


def _save(entries: list[dict], total: int) -> None:
    _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"total": total, "entries": entries}))
    tmp.replace(_STATS_FILE)


def record_completion(job_id: str, title: str | None, status: str) -> None:
    entries, total = _load()
    entries.append({
        "id": job_id,
        "displayName": title or job_id,
        "lastSeen": int(time.time() * 1000),
        "isOnline": False,
        "jobStatus": status,
    })
    _save(entries[-_MAX_ENTRIES:], total + 1)


def get_stats_response() -> dict:
    from app.core.registry import all_jobs

    entries, total = _load()
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
        "jobs_total": total,
        "jobs_last_24h": sum(1 for e in entries if e.get("lastSeen", 0) > cutoff_24h),
    }
