"""Regression test: connectEvents() must not resurrect a stale job after reset().

Bug: the SSE stream for job A drops (network blink / service restart). The
REST probe fails too, so onerror schedules a reconnect via setTimeout (0.5-16s
backoff). While waiting, the user starts job B or opens a track from Recent,
which calls reset(). reset() only closes *the currently assigned*
EventSource -- during the backoff wait that slot is null, so nothing stops
job A's pending timer. The `stopped` flag lives in connectEvents(jobId="A")'s
own closure and reset() has no way to set it. When the timer fires, open()
for A creates a new EventSource, and any in-flight REST probe for A also
calls applyState(s) unconditionally -- overwriting job B's title/BPM/key/
stage with job A's, and wiring up A's audio when A eventually finishes.

The fix must gate every deferred continuation (open(), the onmessage/onerror
handlers, the setTimeout reconnect, and the probeJob() UI write) on the job
still being the current one -- e.g. comparing the closed-over jobId against
the live `currentJobId` state, which reset() already clears to null and the
next submit already reassigns.
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).parent.parent / "static"


def _extract_function(js_text: str, signature: str) -> str:
    """Return the full body (braces included) of a top-level function."""
    start = js_text.index(signature)
    brace_start = js_text.index("{", start)
    depth = 0
    i = brace_start
    while i < len(js_text):
        if js_text[i] == "{":
            depth += 1
        elif js_text[i] == "}":
            depth -= 1
            if depth == 0:
                return js_text[brace_start : i + 1]
        i += 1
    raise AssertionError(f"Unbalanced braces looking for {signature!r}")


def _job_js() -> str:
    return (STATIC / "js" / "job.js").read_text()


def test_probe_job_only_applies_state_for_the_still_current_job():
    """probeJob's applyState(s) write must be skipped once another job has taken over."""
    js = _job_js()
    body = _extract_function(js, "async function probeJob(jobId)")

    assert re.search(
        r"if\s*\(\s*jobId\s*===\s*currentJobId\s*\)\s*\{?\s*applyState\(s\)",
        body,
    ), (
        "probeJob() must only call applyState(s) when `jobId === currentJobId` -- "
        "otherwise a REST probe for a job abandoned by reset() can still paint "
        "its stale title/BPM/key/stage over whatever job is now current."
    )


def test_connect_events_reconnect_is_gated_on_still_being_the_current_job():
    """open()/onerror's backoff reconnect must bail out once the job is no longer current."""
    js = _job_js()
    body = _extract_function(js, "function connectEvents(jobId)")

    # connectEvents must define (or otherwise use) a staleness check that is
    # actually tied to currentJobId -- a `stopped` flag alone is never
    # invalidated by reset(), since reset() has no reference to it.
    assert re.search(r"jobId\s*!==\s*currentJobId|currentJobId\s*!==\s*jobId", body), (
        "connectEvents() must compare the closed-over jobId against the live "
        "`currentJobId` state to detect that reset() (or a new job) has "
        "superseded this connection."
    )
    guard_call_pattern = re.compile(r"\b(\w*[Ss]tale\w*)\s*\(")
    guard_calls = guard_call_pattern.findall(body)
    assert guard_calls, (
        "Expected a staleness-check helper (e.g. `isStale()`) referencing "
        "currentJobId, called from open(), onmessage, onerror and the "
        "backoff timer."
    )
    guard_name = guard_calls[0]

    # open() must refuse to create a new EventSource (and overwrite the
    # shared eventSource slot) for a job reset() has already moved on from.
    open_idx = body.index("const open = () => {")
    new_es_idx = body.index("new EventSource(", open_idx)
    guard_region = body[open_idx:new_es_idx]
    assert guard_name in guard_region, (
        "open() must bail out before constructing a new EventSource when "
        "the job is stale -- otherwise a reconnect timer left over from a "
        "job that reset() already abandoned can still take over the shared "
        "eventSource slot."
    )

    # The setTimeout-scheduled retry must also re-check liveness at fire time,
    # not just a `stopped` flag that reset() can never set (it lives in this
    # job's own closure and reset() has no reference to it).
    timeout_idx = body.index("setTimeout(")
    timeout_call = body[timeout_idx : body.index(");", timeout_idx) + 2]
    assert guard_name in timeout_call, (
        "The backoff setTimeout callback must re-check staleness before "
        "calling open() again -- a bare `stopped` flag is never invalidated "
        "by reset(), so a stale job's reconnect timer fires and resurrects "
        "it after the user has moved to a new job."
    )
