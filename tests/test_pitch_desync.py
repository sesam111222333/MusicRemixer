"""
Regression test: pitch shift must not change playback rate (desync bug).

Before the fix, applyMix() called setPlaybackRate(Math.pow(2, pitch/12), false)
which changes both pitch AND tempo on that stem. A pitched stem then plays
faster or slower than the others, drifting out of sync with the master clock
(which assumes rate=1 for all stems) and inconsistently with the downloaded
remix (which uses asetrate+atempo to shift pitch without changing tempo).

Fix: use AudioBufferSourceNode.detune (in cents) instead.
  pitch_semitones * 100 cents = detune value (e.g. +12 st → 1200 cents)
This shifts pitch without affecting buffer consumption speed → no desync.

Also: _atomicResumeAll in player.js recreates AudioBufferSourceNodes on every
play/seek. Without copying the detune value to the new node, pitch resets to 0
on resume. The fix reads prevDetune from the outgoing bufferNode and applies it
to the new one.
"""

import re
import pathlib

MIXER_JS = pathlib.Path(__file__).parent.parent / "static" / "js" / "mixer.js"
PLAYER_JS = pathlib.Path(__file__).parent.parent / "static" / "js" / "player.js"


def _apply_mix_body():
    src = MIXER_JS.read_text()
    m = re.search(r"export function applyMix\(\)\s*\{(.*?)\n\}", src, re.DOTALL)
    assert m, "applyMix() not found in mixer.js"
    return m.group(1)


def _atomic_resume_body():
    src = PLAYER_JS.read_text()
    m = re.search(r"function _atomicResumeAll\([^)]*\)\s*\{(.*?)\n\}", src, re.DOTALL)
    assert m, "_atomicResumeAll() not found in player.js"
    return m.group(1)


def test_apply_mix_does_not_change_playback_rate():
    """applyMix() must NOT call setPlaybackRate — that changes tempo, desyncing stems."""
    body = _apply_mix_body()
    assert "setPlaybackRate" not in body, (
        "applyMix() calls setPlaybackRate which changes both pitch AND tempo on the "
        "pitched stem. The master clock assumes rate=1 for all stems, so the pitched "
        "stem drifts out of sync. Use bufferNode.detune (cents) instead."
    )


def test_apply_mix_uses_detune_for_pitch():
    """applyMix() must apply pitch via AudioBufferSourceNode.detune (pitch-only, no tempo change)."""
    body = _apply_mix_body()
    assert "detune" in body, (
        "applyMix() does not use detune for pitch shifting. "
        "Set bufferNode.detune.value = pitch_semitones * 100 (cents) — "
        "this shifts pitch without affecting playback speed, preserving sync."
    )


def test_atomic_resume_preserves_detune():
    """_atomicResumeAll must copy detune from the outgoing bufferNode to the new one.

    Without this, any pause/seek/resume resets detune to 0 on the fresh node
    even though applyMix() just set the correct pitch. After resume the stem
    would play at pitch=0 until the next applyMix() call.
    """
    body = _atomic_resume_body()
    assert "detune" in body, (
        "_atomicResumeAll() does not reference detune. "
        "When creating a new AudioBufferSourceNode, read prevDetune from the "
        "outgoing el.bufferNode.detune.value and apply it to the new node so "
        "pitch is preserved across pause/seek/resume."
    )
