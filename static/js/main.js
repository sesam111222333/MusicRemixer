import {
  playBtn, loopBtn, multitrack, loopEnabled, loopStart, loopEnd,
  setLoopStart, setLoopEnd, selectedStems, saveSelectedStems,
  selectedBackend, setSelectedBackend, getActiveStemNames,
  form,
} from "./state.js";
import { renderEmptyShell } from "./player.js";
import { wireJobForm } from "./job.js";
import { wireTransportButtons } from "./transport.js";
import { togglePlayPause, updateLoopRegionVisual } from "./transport.js";
import { wireStemListControls, wireMixerToolbar } from "./mixer.js";

// ─── Backend model selector ───

function updateBackendUI() {
  form.dataset.backend = selectedBackend;
  for (const btn of document.querySelectorAll(".backend-tab")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.backend === selectedBackend));
  }
}

function wireBackendButtons() {
  updateBackendUI();
  for (const btn of document.querySelectorAll(".backend-tab")) {
    btn.addEventListener("click", () => {
      if (btn.dataset.backend === selectedBackend) return;
      setSelectedBackend(btn.dataset.backend);
      // Drop stems that don't exist in the new backend.
      const validNames = getActiveStemNames();
      for (const stem of [...selectedStems]) {
        if (!validNames.includes(stem)) selectedStems.delete(stem);
      }
      if (selectedStems.size === 0) {
        for (const n of validNames) selectedStems.add(n);
      }
      saveSelectedStems();
      updateBackendUI();
      refreshStemChoiceVisuals();
    });
  }
}

// ─── Stem choice toggles on the import page ───
//
// Filter-chip semantics (Spotify-style). The natural mental model when
// a user sees all stems lit up is "everything is extracted"; when
// they then click ONE chip, they expect "now only this one". A plain
// toggle inverts the clicked chip and leaves the others on, which
// reads as "I just deselected the one I wanted" -- exactly the user
// confusion that prompted this fix.
//
// Algorithm:
//  - "All selected" is the implicit default (no filter applied).
//  - First click on a chip while in default state switches to
//    "only this stem" (clears all others).
//  - Subsequent clicks on inactive chips ADD them to the filter.
//  - Clicks on the only-selected chip clear it; if that empties the
//    selection, we revert to "all selected" (wraparound).
//
// Persisted across reloads so the next song honors the user's last
// chosen subset, but a 0-selection state is normalized to all stems.
function refreshStemChoiceVisuals() {
  for (const btn of document.querySelectorAll(".stem-choice[data-stem]")) {
    btn.setAttribute(
      "aria-pressed",
      String(selectedStems.has(btn.dataset.stem)),
    );
  }
}

function handleStemChoiceClick(stem) {
  const stemNames = getActiveStemNames();
  const allSelected = stemNames.every((n) => selectedStems.has(n));
  if (allSelected) {
    selectedStems.clear();
    selectedStems.add(stem);
  } else if (selectedStems.has(stem)) {
    selectedStems.delete(stem);
    if (selectedStems.size === 0) {
      for (const n of stemNames) selectedStems.add(n);
    }
  } else {
    selectedStems.add(stem);
  }
  saveSelectedStems();
  refreshStemChoiceVisuals();
}

function wireStemChoiceButtons() {
  refreshStemChoiceVisuals();
  for (const btn of document.querySelectorAll(".stem-choice[data-stem]")) {
    btn.addEventListener("click", () => handleStemChoiceClick(btn.dataset.stem));
  }
}

// ─── Wire everything up ───

wireJobForm();
wireTransportButtons();
wireStemListControls();
wireMixerToolbar();
wireStemChoiceButtons();
wireBackendButtons();

// ─── Keyboard shortcuts ───

document.addEventListener("keydown", (e) => {
  if (!multitrack) return;
  if (e.target instanceof HTMLInputElement) return;
  if (e.code === "Space") {
    e.preventDefault();
    togglePlayPause();
  } else if (e.code === "BracketLeft") {
    e.preventDefault();
    multitrack.setTime(Math.max(0, multitrack.getCurrentTime() - 5));
  } else if (e.code === "BracketRight") {
    e.preventDefault();
    multitrack.setTime(
      Math.min(multitrack.getDuration(), multitrack.getCurrentTime() + 5),
    );
  } else if (e.code === "KeyL") {
    e.preventDefault();
    loopBtn.click();
  } else if (e.code === "KeyI" && loopEnabled && multitrack) {
    e.preventDefault();
    setLoopStart(Math.min(multitrack.getCurrentTime(), loopEnd - 0.5));
    updateLoopRegionVisual();
  } else if (e.code === "KeyO" && loopEnabled && multitrack) {
    e.preventDefault();
    setLoopEnd(Math.max(multitrack.getCurrentTime(), loopStart + 0.5));
    updateLoopRegionVisual();
  }
});

// ─── Bootstrap ───

renderEmptyShell();
