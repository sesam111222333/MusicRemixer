from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

from app.core.config import BSROFORMER_MODEL, DEMUCS_DEVICE, DEMUCS_MODEL
from app.core.models import Job, JobCancelled
from app.core.registry import set_proc

logger = logging.getLogger("stemdeck.pipeline")

_PCT_RE = re.compile(r"(\d{1,3})%")


def separate(job: Job, source: Path, job_dir: Path) -> Path:
    if job.backend == "bsroformer":
        return _separate_bsroformer(job, source, job_dir)
    return _separate_demucs(job, source, job_dir)


def _separate_demucs(job: Job, source: Path, job_dir: Path) -> Path:
    from app.pipeline.download import _set

    _set(job, status="separating", progress=0.0, stage="Separating stems...")

    cmd = [
        sys.executable,
        "-m",
        "demucs",
        "-n",
        DEMUCS_MODEL,
        "-d",
        DEMUCS_DEVICE,
        "-o",
        str(job_dir),
        str(source),
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=0,
    )
    # Register immediately so a concurrent cancel can terminate the process.
    set_proc(job.id, proc)

    buf = ""
    tail: list[str] = []
    try:
        if proc.stderr is None:
            raise RuntimeError("demucs subprocess has no stderr pipe")
        # Cancel may have arrived in the window before registration above.
        if job.cancel_requested:
            proc.terminate()
        while True:
            ch = proc.stderr.read(1)
            if not ch:
                break
            if ch in ("\r", "\n"):
                line = buf.strip()
                buf = ""
                if not line:
                    continue
                m = _PCT_RE.search(line)
                if m:
                    pct = max(0, min(100, int(m.group(1))))
                    _set(job, progress=pct / 100.0, stage=f"Separating {pct}%")
                else:
                    tail.append(line)
                    if len(tail) > 40:
                        tail.pop(0)
            else:
                buf += ch

        proc.wait()
    finally:
        set_proc(job.id, None)

    if job.cancel_requested:
        raise JobCancelled()
    if proc.returncode != 0:
        detail = "\n".join(tail[-15:]) if tail else "(no stderr captured)"
        logger.error("demucs exited %s; tail:\n%s", proc.returncode, detail)
        last = tail[-1] if tail else f"exit status {proc.returncode}"
        raise RuntimeError(f"demucs failed: {last}")

    stems_root = job_dir / DEMUCS_MODEL / source.stem
    if not stems_root.is_dir():
        raise RuntimeError(f"demucs output not found at {stems_root}")
    return stems_root


def _run_demucs_on_file(job: Job, source: Path, out_dir: Path, model: str, progress_offset: float) -> Path:
    """Run demucs on *source*, write output to *out_dir*, report progress
    scaled to the range [progress_offset, progress_offset + 0.5]."""
    from app.pipeline.download import _set

    cmd = [
        sys.executable, "-m", "demucs",
        "-n", model,
        "-d", DEMUCS_DEVICE,
        "-o", str(out_dir),
        str(source),
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=0,
    )
    # Register immediately so a concurrent cancel can terminate the process.
    set_proc(job.id, proc)

    buf = ""
    tail: list[str] = []
    try:
        if proc.stderr is None:
            raise RuntimeError("demucs subprocess has no stderr pipe")
        # Cancel may have arrived in the window before registration above.
        if job.cancel_requested:
            proc.terminate()
        while True:
            ch = proc.stderr.read(1)
            if not ch:
                break
            if ch in ("\r", "\n"):
                line = buf.strip()
                buf = ""
                if not line:
                    continue
                m = _PCT_RE.search(line)
                if m:
                    pct = max(0, min(100, int(m.group(1))))
                    stage_pct = progress_offset + pct / 200.0
                    _set(job, progress=stage_pct, stage=f"Separating instruments {pct}%")
                else:
                    tail.append(line)
                    if len(tail) > 40:
                        tail.pop(0)
            else:
                buf += ch
        proc.wait()
    finally:
        set_proc(job.id, None)

    if job.cancel_requested:
        raise JobCancelled()
    if proc.returncode != 0:
        detail = "\n".join(tail[-15:]) if tail else "(no stderr captured)"
        logger.error("demucs (inst stage) exited %s; tail:\n%s", proc.returncode, detail)
        last = tail[-1] if tail else f"exit status {proc.returncode}"
        raise RuntimeError(f"demucs (instrument stage) failed: {last}")

    # Demucs names the output directory after the input file's stem.
    # Scan instead of constructing the path to be robust against any
    # filename sanitization Demucs might apply.
    model_dir = out_dir / model
    if not model_dir.is_dir():
        raise RuntimeError(f"Demucs model output dir not found: {model_dir}")
    candidates = [d for d in model_dir.iterdir() if d.is_dir()]
    if not candidates:
        raise RuntimeError(f"No stem directories found in {model_dir}")
    stems_root = max(candidates, key=lambda d: d.stat().st_mtime)
    return stems_root


def _separate_bsroformer(job: Job, source: Path, job_dir: Path) -> Path:
    """Two-stage separation:
    1. BS-RoFormer (audio-separator): source → vocals.wav + instrumental.wav
    2. Demucs htdemucs_ft on the instrumental → drums.wav + bass.wav + other.wav

    Assembles the four stems into job_dir/_bsr_stems/ and returns that path.

    Stage 1 runs as a subprocess (_bsr_worker.py) so the cancel API can
    terminate it at any point — audio_separator.sep.separate() is a blocking
    in-process call with no cancellation hook of its own.
    """
    from app.pipeline.download import _set

    _set(job, status="separating", progress=0.0, stage="Separating vocals (BS-RoFormer)...")

    bsr_tmp = job_dir / "_bsr_tmp"
    bsr_tmp.mkdir(exist_ok=True)

    # Stage 1: run audio-separator as a subprocess so cancel can terminate it.
    worker = Path(__file__).parent / "_bsr_worker.py"
    cmd = [
        sys.executable, str(worker),
        "--model", BSROFORMER_MODEL,
        "--output-dir", str(bsr_tmp),
        str(source),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # Register immediately so a concurrent cancel can terminate the process.
    set_proc(job.id, proc)
    try:
        # Cancel may have arrived in the window before registration above.
        if job.cancel_requested:
            proc.terminate()
        stdout, stderr = proc.communicate()
    finally:
        set_proc(job.id, None)

    if job.cancel_requested:
        raise JobCancelled()
    if proc.returncode != 0:
        detail = stderr.strip()[-500:] or "(no stderr)"
        raise RuntimeError(f"audio-separator failed (exit {proc.returncode}): {detail}")

    try:
        output_files: list[str] = json.loads(stdout.strip())
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"audio-separator output could not be parsed: {stdout!r}") from exc

    _set(job, progress=0.45, stage="Vocals done — separating instruments...")

    if job.cancel_requested:
        raise JobCancelled()

    # Identify vocals and instrumental output files.
    # audio-separator may return bare filenames (relative) or full paths.
    vocals_path: Path | None = None
    instrumental_path: Path | None = None
    for f in output_files:
        p = Path(f)
        if not p.is_absolute():
            p = bsr_tmp / p
        name_lower = p.name.lower()
        if "(instrumental)" in name_lower or "(no_vocals)" in name_lower or "(no vocals)" in name_lower:
            instrumental_path = p
        elif "(vocals)" in name_lower:
            vocals_path = p

    if vocals_path is None or instrumental_path is None:
        raise RuntimeError(
            f"BS-RoFormer output could not be identified. Got: {[str(f) for f in output_files]}"
        )

    logger.info("BSR vocals: %s  instrumental: %s", vocals_path.name, instrumental_path.name)

    # Stage 2: Demucs on the instrumental to get drums/bass/other.
    # Rename to a plain filename first — Demucs uses the input stem as its
    # output directory name and may choke on special chars / long names.
    demucs_tmp = job_dir / "_demucs_tmp"
    demucs_tmp.mkdir(exist_ok=True)
    plain_instrumental = demucs_tmp / "instrumental.wav"
    shutil.copy2(str(instrumental_path), plain_instrumental)

    inst_model = "htdemucs_ft"
    inst_stems_root = _run_demucs_on_file(job, plain_instrumental, demucs_tmp, inst_model, 0.45)

    # Assemble the four stems into a clean output directory.
    stems_root = job_dir / "_bsr_stems"
    stems_root.mkdir(exist_ok=True)

    shutil.copy2(str(vocals_path), stems_root / "vocals.wav")
    for name in ("drums", "bass", "other"):
        src = inst_stems_root / f"{name}.wav"
        if src.exists():
            shutil.copy2(str(src), stems_root / f"{name}.wav")
        else:
            logger.warning("Expected demucs stem missing: %s", src)

    shutil.rmtree(bsr_tmp, ignore_errors=True)
    shutil.rmtree(demucs_tmp, ignore_errors=True)

    return stems_root
