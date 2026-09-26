"""Pitch is an export-only setting — keep playback and UI consistent with that.

Web Audio has no speed-preserving pitch on AudioBufferSourceNode: both
playbackRate and detune feed computedPlaybackRate = rate * 2^(detune/1200),
so either one makes the pitched stem run faster/slower and drift out of sync
with the master clock. Earlier fixes flipped between "apply live" and "don't";
this pins the decision (2026-09-26): pitch is rendered server-side into
remix.wav (asetrate+atempo) and the slider says so. Changing it requires a real
pitch shifter (AudioWorklet), not detune/playbackRate.

These are source checks (there is no JS test runner in this repo).
"""

import pathlib
import re

JS = pathlib.Path(__file__).parent.parent / "static" / "js"


def _fn_body(src: str, header: str) -> str:
    m = re.search(header + r"\s*\{(.*?)\n\}", src, re.DOTALL)
    assert m, f"{header} not found"
    return m.group(1)


def test_playback_never_changes_rate_for_pitch():
    mixer = (JS / "mixer.js").read_text()
    player = (JS / "player.js").read_text()
    apply_mix = _fn_body(mixer, r"export function applyMix\(\)")
    resume = _fn_body(player, r"function _atomicResumeAll\([^)]*\)")
    for body in (apply_mix, resume):
        for forbidden in ("detune", "playbackRate", "setPlaybackRate"):
            assert forbidden not in body, f"{forbidden} changes speed and desyncs the stem"


def test_pitch_control_is_labelled_as_export_only():
    mixer = (JS / "mixer.js").read_text()
    body = _fn_body(mixer, r"function makePitchControl\([^)]*\)")
    assert 'textContent = "export"' in body
    assert "not heard in playback" in body


def test_downloaded_mix_carries_pitch():
    player = (JS / "player.js").read_text()
    assert "pitches=${encodeURIComponent(pitchParam)}" in player
