import {
  playBtn, loopBtn, multitrack, loopEnabled, loopStart, loopEnd,
  setLoopStart, setLoopEnd,
} from "./state.js";
import { renderEmptyShell, rearmLoopWrap } from "./player.js";
import { wireJobForm } from "./job.js";
import { wireTransportButtons } from "./transport.js";
import { togglePlayPause, updateLoopRegionVisual, applyWaveZoom } from "./transport.js";
import { wireStemListControls, wireMixerToolbar } from "./mixer.js";

// ─── Wire everything up ───

wireJobForm();
wireTransportButtons();
wireStemListControls();
wireMixerToolbar();

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
    rearmLoopWrap();
  } else if (e.code === "KeyO" && loopEnabled && multitrack) {
    e.preventDefault();
    setLoopEnd(Math.max(multitrack.getCurrentTime(), loopStart + 0.5));
    updateLoopRegionVisual();
    rearmLoopWrap();
  }
});

// Sync loop button and waveform visuals after a session import.
document.addEventListener("stemdeck:session-imported", () => {
  loopBtn.classList.toggle("active", loopEnabled);
  updateLoopRegionVisual();
  applyWaveZoom();
  rearmLoopWrap();
});

// ─── Bootstrap ───

renderEmptyShell();
