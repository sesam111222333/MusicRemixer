"""
Regression test: pitch slider must affect live playback (applyMix).

Before the fix, applyMix() only called setTrackVolume and never touched
state.pitch, so the pitch slider had no audible effect during playback.
The slider also lacked an applyMix() call in its input handler.
"""

import re
import pathlib

MIXER_JS = pathlib.Path(__file__).parent.parent / "static" / "js" / "mixer.js"


def _src():
    return MIXER_JS.read_text()


def _apply_mix_body(src):
    m = re.search(r"export function applyMix\(\)\s*\{(.*?)\n\}", src, re.DOTALL)
    assert m, "applyMix() not found in mixer.js"
    return m.group(1)


def _set_pitch_body(src):
    m = re.search(r"export function setPitch\([^)]*\)\s*\{(.*?)\n\}", src, re.DOTALL)
    assert m, "setPitch() not found in mixer.js"
    return m.group(1)


def _pitch_slider_input_handler(src):
    """Return the body of the pitch slider's 'input' event listener."""
    m = re.search(
        r'slider\.addEventListener\("input",\s*\(\)\s*=>\s*\{(.*?)\}\)',
        src,
        re.DOTALL,
    )
    assert m, "pitch slider input handler not found in mixer.js"
    return m.group(1)


def test_apply_mix_does_not_apply_pitch_live():
    """applyMix() must NOT apply pitch to the live bufferNode (neither detune nor setPlaybackRate).

    Both detune and setPlaybackRate change computedPlaybackRate, desyncing the
    pitched stem from the master clock. Pitch is preserved in mixerState so
    that the export (remix.wav) can apply it correctly with FFmpeg asetrate+atempo.
    """
    body = _apply_mix_body(_src())
    assert "detune" not in body, (
        "applyMix() sets bufferNode.detune which changes computedPlaybackRate = "
        "rate * 2^(detune/1200), desyncing the stem. Remove the detune assignment."
    )
    assert "setPlaybackRate" not in body, (
        "applyMix() calls setPlaybackRate which changes both pitch AND tempo. "
        "This desyncs the pitched stem. Remove the setPlaybackRate call."
    )


def test_pitch_slider_input_calls_apply_mix():
    """Dragging the pitch slider must trigger applyMix() for an immediate live update."""
    handler = _pitch_slider_input_handler(_src())
    assert "applyMix" in handler, (
        "The pitch slider's 'input' event handler does not call applyMix(). "
        "Dragging the slider updates state.pitch but the change is never applied to playback."
    )


def test_set_pitch_calls_apply_mix():
    """setPitch() (used on double-click reset) must call applyMix() so the reset is heard."""
    body = _set_pitch_body(_src())
    assert "applyMix" in body, (
        "setPitch() does not call applyMix(). "
        "Double-clicking to reset pitch updates the state but is never applied to playback."
    )
