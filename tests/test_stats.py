from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import app.core.stats as stats_module
from app.core.stats import _load, _save, record_completion, get_stats_response


@pytest.fixture(autouse=True)
def _isolated_stats_file(tmp_path):
    fake_file = tmp_path / "job_stats.json"
    with patch.object(stats_module, "_STATS_FILE", fake_file):
        yield fake_file


def test_load_returns_empty_list_when_file_missing(tmp_path):
    missing = tmp_path / "nonexistent.json"
    with patch.object(stats_module, "_STATS_FILE", missing):
        entries, total = _load()
        assert entries == []
        assert total == 0


def test_save_then_load_roundtrip():
    entries = [{"id": "abc", "jobStatus": "done"}]
    _save(entries, 42)
    loaded_entries, loaded_total = _load()
    assert loaded_entries == entries
    assert loaded_total == 42


def test_load_handles_legacy_list_format(_isolated_stats_file):
    """Files written by the old code (plain JSON array) must still load."""
    legacy = [{"id": "old", "jobStatus": "done"}]
    _isolated_stats_file.write_text(json.dumps(legacy))
    entries, total = _load()
    assert entries == legacy
    assert total == len(legacy)


def test_save_is_atomic_under_concurrent_read(tmp_path, _isolated_stats_file):
    """_save must never leave the file in a truncated state that causes _load
    to return an empty list while a concurrent reader is mid-read.

    Strategy: perform many write/read pairs from two threads and assert that
    _load never returns [] after at least one record has been written.
    """
    initial = [{"id": str(i), "jobStatus": "done"} for i in range(50)]
    _save(initial, len(initial))

    errors = []
    stop = threading.Event()

    def writer():
        extra = list(initial)
        for i in range(50, 150):
            extra.append({"id": str(i), "jobStatus": "done"})
            capped = extra[-stats_module._MAX_ENTRIES :]
            _save(capped, len(extra))

    def reader():
        while not stop.is_set():
            entries, _total = _load()
            if entries == []:
                errors.append("_load returned [] while data existed on disk")

    t_reader = threading.Thread(target=reader, daemon=True)
    t_writer = threading.Thread(target=writer)

    t_reader.start()
    t_writer.start()
    t_writer.join()
    stop.set()
    t_reader.join(timeout=2)

    assert not errors, f"Race condition detected: {errors[0]}"


def test_jobs_total_exceeds_max_entries():
    """jobs_total must keep growing past _MAX_ENTRIES even though only the
    most-recent _MAX_ENTRIES entries are kept on disk."""
    over = stats_module._MAX_ENTRIES + 5
    for i in range(over):
        record_completion(f"job-{i}", f"Job {i}", "done")

    with patch("app.core.registry.all_jobs", return_value={}):
        result = get_stats_response()

    assert result["jobs_total"] == over, (
        f"Expected jobs_total={over}, got {result['jobs_total']} — "
        "total is capped by _MAX_ENTRIES"
    )
