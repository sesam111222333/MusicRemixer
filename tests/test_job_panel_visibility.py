"""Regression test: job progress panel must be visible while a job is running.

Previously job.css set `.job { display: none !important }` which permanently
hid the panel regardless of the `.hidden` class toggle in JS.  And the submit
handler called `jobBox.classList.add("hidden")` instead of `.remove("hidden")`,
so the panel was never revealed after submit.
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).parent.parent / "static"


def _extract_css_rule(css_text: str, selector: str) -> str:
    """Return the body of the first rule that matches `selector { ... }`."""
    pattern = re.compile(
        re.escape(selector) + r"\s*\{([^}]*)\}",
        re.DOTALL,
    )
    m = pattern.search(css_text)
    assert m, f"Selector {selector!r} not found in CSS"
    return m.group(1)


def test_job_css_class_does_not_hide_panel():
    """`.job` must not carry `display: none` — visibility is controlled by `.hidden`."""
    css = (STATIC / "css" / "job.css").read_text()
    rule_body = _extract_css_rule(css, ".job")
    assert "display" not in rule_body, (
        ".job rule must not set `display` — use the .hidden class to toggle visibility"
    )


def test_submit_handler_reveals_job_box():
    """After a successful job POST the submit handler must *remove* `.hidden` from jobBox."""
    js = (STATIC / "js" / "job.js").read_text()

    # Find the block after setCurrentJobId — that is the submit-handler reveal sequence.
    idx = js.index("setCurrentJobId(jobId)")
    snippet = js[idx : idx + 300]

    assert "jobBox.classList.remove" in snippet, (
        "Submit handler must call jobBox.classList.remove('hidden') to reveal the progress panel"
    )
    assert "jobBox.classList.add" not in snippet, (
        "Submit handler must NOT call jobBox.classList.add('hidden') right after setCurrentJobId — "
        "that keeps the panel permanently hidden"
    )
