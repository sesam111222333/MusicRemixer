from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


class JobCancelled(Exception):
    """Raised inside a pipeline stage when the job's cancel flag is set."""


@dataclass
class Job:
    id: str
    status: str = (
        "queued"  # queued | downloading | analyzing | separating | done | error | cancelled
    )
    progress: float = 0.0
    stage_message: str = "Queued"
    title: str | None = None
    duration_sec: float | None = None
    thumbnail: str | None = None
    bpm: int | None = None
    key: str | None = None
    scale: str | None = None  # "Major" / "Natural Minor"
    key_confidence: int | None = None  # 0-100 percent
    lufs: float | None = None  # ITU-R BS.1770 integrated loudness (dB)
    peak_db: float | None = None  # sample peak in dBFS (close to true peak)
    stems: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    # Set by POST /api/jobs/{id}/cancel; consumed by pipeline stages.
    # Not surfaced via to_state() -- it's internal control state.
    cancel_requested: bool = False
    # Wall-clock timestamps for metadata-based sweep -- more predictable
    # than directory mtime, which can be touched by unrelated FS events.
    created_at: float = field(default_factory=time.time)

    def to_state(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage_message,
            "title": self.title,
            "duration": self.duration_sec,
            "thumbnail": self.thumbnail,
            "bpm": self.bpm,
            "key": self.key,
            "scale": self.scale,
            "key_confidence": self.key_confidence,
            "lufs": self.lufs,
            "peak_db": self.peak_db,
            "stems": self.stems,
            "error": self.error,
            "created_at": self.created_at,
        }
