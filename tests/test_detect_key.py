from __future__ import annotations

"""Unit tests for _detect_key confidence calculation.

The runner_up used for confidence MUST exclude the relative major/minor of
the winner (e.g. A min for C maj, C maj for A min).  Those keys share all
diatonic tones, so they always score close to the winner and tell us nothing
about real tonal ambiguity.  If they are NOT excluded, confidence collapses
to near-zero for songs that actually have a clear key.
"""

from app.pipeline.analyze import _detect_key


# ---------------------------------------------------------------------------
# Test 1 — relative key must be excluded from runner_up (confidence too low)
#
# Chroma: C major family, A prominent (vi chord common).
# At A=0.85, the old code picks A min as runner_up because it scores second
# (shares all diatonic tones with C maj).  The C-maj / A-min gap is tiny →
# confidence collapses to 33 % even though the key is unambiguous.
# After the fix, runner_up = G maj (genuine competitor) → confidence = 100 %.
# ---------------------------------------------------------------------------

def test_relative_key_excluded_from_runner_up_confidence():
    """Confidence must NOT collapse because the relative key is runner-up."""
    # C-major chroma: C (tonic) strong, A prominent (vi chord) — exactly the
    # case that previously let A min become runner-up and kill confidence.
    chroma = [0.02] * 12
    chroma[0] = 0.90   # C — tonic
    chroma[2] = 0.55   # D
    chroma[4] = 0.68   # E
    chroma[5] = 0.45   # F
    chroma[7] = 0.78   # G — dominant
    chroma[9] = 0.85   # A — intentionally prominent; triggers the relative-key bug
    chroma[11] = 0.52  # B

    label, _, confidence = _detect_key(chroma)

    # Winner must be in the C major / A minor diatonic family.
    assert label in ("C maj", "A min"), f"Unexpected winner: {label!r}"

    # Old code: runner_up = A min (relative of C maj), confidence ≈ 33 %.
    # Fixed code: runner_up = G maj (non-relative), confidence = 100 %.
    assert confidence > 50, (
        f"confidence={confidence}% is too low for a clear diatonic chroma. "
        f"The relative key must be excluded from the runner-up so the gap "
        "reflects genuine tonal ambiguity, not the inherent correlation between "
        "relative keys."
    )


# ---------------------------------------------------------------------------
# Test 2 — confidence must not be negative when tie-break selects minor
#
# When best_maj and best_min are within _MINOR_TIE_BREAK_FRAC, winner is
# forced to best_min even if best_maj scored higher.  Old code then picks
# the relative major (= best_maj, with a higher score) as runner_up →
# confidence_score < 0 → clamped to 0 %.
#
# Chroma: C major family, A at 0.87 — A min wins by tie-break, C maj
# (relative, higher score) becomes runner_up → confidence = 0.
# After the fix: C maj is excluded from runner_up, confidence > 0.
# ---------------------------------------------------------------------------

def test_confidence_non_negative_when_tie_break_selects_minor():
    """confidence_pct must be > 0 even when minor wins by tie-break heuristic."""
    # Same C-major family chroma but A slightly stronger than in test 1.
    # This pushes the weighted scores of C maj and A min into tie-break range,
    # causing winner = A min (minor preference), runner_up = C maj (higher score)
    # → negative confidence → clamped to 0 under the old code.
    chroma = [0.02] * 12
    chroma[0] = 0.90   # C
    chroma[2] = 0.55   # D
    chroma[4] = 0.68   # E
    chroma[5] = 0.45   # F
    chroma[7] = 0.78   # G
    chroma[9] = 0.87   # A — slightly stronger than in test 1; triggers tie-break + negative conf
    chroma[11] = 0.52  # B

    _, _, confidence = _detect_key(chroma)

    # Old code: winner = A min (tie-break), runner_up = C maj (relative, higher score)
    # → confidence_score < 0 → clamped to 0.
    # Fixed code: C maj (relative) excluded → runner_up = G maj → confidence > 0.
    assert confidence > 0, (
        f"confidence={confidence}% — the tie-break winner scored lower than its "
        "relative key runner-up, yielding a negative confidence that was clamped to 0. "
        "The relative key must be excluded from runner-up selection."
    )
