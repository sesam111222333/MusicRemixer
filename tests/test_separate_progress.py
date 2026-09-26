"""htdemucs_ft is a bag of four models and demucs prints one 0→100 % tqdm per
model. Mapping each run straight onto the progress bar made it jump backwards
(0.89 → 0.74 → 0.95 → 0.775 in production). Progress must only move forward
and end at 1.0 after the last model."""
from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

from app.core.models import Job
from app.pipeline.separate import _run_demucs_on_file, separate


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


class _FakeBsrWorker:
    """Stands in for _bsr_worker.py: tqdm on stderr, JSON of outputs on stdout."""

    def __init__(self, cmd, **kwargs):
        out_dir = Path(cmd[cmd.index("--output-dir") + 1])
        vocals, inst = out_dir / "src_(Vocals).wav", out_dir / "src_(Instrumental).wav"
        vocals.write_bytes(b"RIFF")
        inst.write_bytes(b"RIFF")
        self.stderr = io.StringIO("".join(f"\r {p}%|" for p in (0, 25, 50, 75, 100)) + "\n")
        self.stdout = io.StringIO(json.dumps([str(vocals), str(inst)]))
        self.returncode = 0

    def wait(self):
        return 0

    def poll(self):
        return 0

    def terminate(self):
        pass


def test_vocal_stage_reports_progress_and_whole_run_is_monotonic(tmp_path):
    """The BS-RoFormer stage used to sit at 0 % for its whole run (~35 s of a
    60 s job) because the worker's stderr was only read at the end."""
    job = Job(id="progress0002")
    seen: list[float] = []
    orig_setattr = Job.__setattr__

    def spy(self, name, value):
        if name == "progress":
            seen.append(value)
        orig_setattr(self, name, value)

    def popen(cmd, **kwargs):
        return _FakeBsrWorker(cmd) if "--output-dir" in cmd else _FakeDemucs(cmd)

    with patch("app.pipeline.separate.subprocess.Popen", side_effect=popen), \
         patch.object(Job, "__setattr__", spy):
        separate(job, tmp_path / "source.wav", tmp_path)

    vocal_stage = [p for p in seen if 0 < p < 0.45]
    assert vocal_stage, f"no progress during the vocal stage: {seen}"
    assert all(b >= a for a, b in zip(seen, seen[1:])), f"progress went backwards: {seen}"
    assert abs(seen[-1] - 1.0) < 1e-9
