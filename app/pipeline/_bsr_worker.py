"""Subprocess worker: runs audio-separator in-process so the parent pipeline
can terminate it via Popen.terminate() when a cancel is requested.

Stdout: JSON array of output file paths.
Stderr: audio-separator log output.
Exit code: 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("source")
    args = p.parse_args()

    try:
        from audio_separator.separator import Separator
    except ImportError:
        print(
            "audio-separator is not installed. "
            "Run: pip install audio-separator[gpu]",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        sep = Separator(output_dir=args.output_dir, output_format="WAV", log_level=logging.WARNING)
    except TypeError:
        sep = Separator(output_dir=args.output_dir, output_format="WAV")

    sep.load_model(model_filename=args.model)
    output_files = sep.separate(args.source)
    print(json.dumps(output_files))


if __name__ == "__main__":
    main()
