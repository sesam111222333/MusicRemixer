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


def test_apply_mix_does_not_use_detune_for_pitch():
    """applyMix() must NOT apply pitch via AudioBufferSourceNode.detune.

    detune changes computedPlaybackRate = rate * 2^(detune/1200), so it alters
    both pitch AND tempo — the same desync bug as setPlaybackRate. Pitch is
    stored in mixerState for export (remix.wav asetrate+atempo) only.
    """
    body = _apply_mix_body()
    assert "detune" not in body, (
        "applyMix() sets bufferNode.detune which changes computedPlaybackRate = "
        "rate * 2^(detune/1200). This desyncs the pitched stem from the master "
        "clock. Remove all detune assignments from applyMix()."
    )


def test_atomic_resume_does_not_preserve_detune():
    """_atomicResumeAll must NOT copy detune from the outgoing bufferNode.

    Since applyMix() no longer sets detune, propagating a stale detune value
    from the old node to the new one would re-introduce tempo desync on every
    pause/seek/resume.
    """
    body = _atomic_resume_body()
    assert "prevDetune" not in body, (
        "_atomicResumeAll() copies prevDetune to the new bufferNode. "
        "applyMix() no longer sets detune, so this propagates stale values. "
        "Remove the prevDetune read and write."
    )


def test_apply_mix_does_not_set_detune_on_buffer_node():
    """applyMix() must NOT set bufferNode.detune — detune changes computedPlaybackRate = rate * 2^(detune/1200).

    A stem with +12 semitones of pitch plays at 2× speed, desyncing from the
    master clock (which assumes rate=1 for all stems). This is the same bug as
    the setPlaybackRate variant. Pitch must be stored in state for export only
    (remix.wav uses FFmpeg asetrate+atempo for correct pitch-without-tempo-change).
    """
    body = _apply_mix_body()
    assert "detune" not in body, (
        "applyMix() sets bufferNode.detune which changes computedPlaybackRate = "
        "rate * 2^(detune/1200). A stem pitched +12 st plays at 2× speed, "
        "desyncing from the master clock. Remove the detune assignment; "
        "pitch shift is only valid for the downloaded remix (asetrate+atempo)."
    )


def test_atomic_resume_does_not_copy_detune():
    """_atomicResumeAll must NOT copy prevDetune to the new bufferNode.

    Since applyMix() no longer sets detune on the bufferNode, copying the
    outgoing node's detune value to the new node would perpetuate a stale
    non-zero detune and re-introduce the tempo-desync bug on every resume.
    """
    body = _atomic_resume_body()
    assert "prevDetune" not in body, (
        "_atomicResumeAll() copies prevDetune from the old bufferNode to the new one. "
        "Since applyMix() no longer sets detune, this propagates stale detune "
        "and re-introduces the desync. Remove the prevDetune copy."
    )
