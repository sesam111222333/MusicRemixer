"""htdemucs_ft is a bag of four models and demucs prints one 0→100 % tqdm per
model. Mapping each run straight onto the progress bar made it jump backwards
(0.89 → 0.74 → 0.95 → 0.775 in production). Progress must only move forward
and end at 1.0 after the last model."""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

from app.core.models import Job
from app.pipeline.separate import _run_demucs_on_file


class _FakeDemucs:
    def __init__(self, cmd, **kwargs):
        out_dir = Path(cmd[cmd.index("-o") + 1])
        model = cmd[cmd.index("-n") + 1]
        (out_dir / model / "instrumental").mkdir(parents=True, exist_ok=True)
        runs = "".join(f"\r {p}%|" for _ in range(4) for p in (0, 25, 50, 75, 100))
        self.stderr = io.StringIO(runs + "\n")
        self.returncode = 0

    def wait(self):
        return 0

    def poll(self):
        return 0

    def terminate(self):
        pass


def test_progress_is_monotonic_across_the_model_bag(tmp_path):
    job = Job(id="progress0001")
    seen: list[float] = []
    orig_setattr = Job.__setattr__

    def spy(self, name, value):
        if name == "progress":
            seen.append(value)
        orig_setattr(self, name, value)

    with patch("app.pipeline.separate.subprocess.Popen", _FakeDemucs), \
         patch.object(Job, "__setattr__", spy):
        _run_demucs_on_file(job, tmp_path / "instrumental.wav", tmp_path, "htdemucs_ft", 0.45)

    assert seen, "no progress reported"
    assert all(b >= a for a, b in zip(seen, seen[1:])), f"progress went backwards: {seen}"
    assert abs(seen[-1] - 1.0) < 1e-9, seen[-1]
