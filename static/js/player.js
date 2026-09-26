import Multitrack from "/vendor/multitrack.js";
import { fmtTime } from "./utils.js";
import {
  STEM_NAMES, STEM_COLORS, PROGRESS_COLOR,
  LOOP_DEFAULT_START_FRAC, LOOP_DEFAULT_END_FRAC,
} from "./constants.js";
import {
  mixerEl, multitrackContainer, bpmChip, keyChip, stemsChip, timeEl,
  titleEl, npThumb, rulerTime, wavesGrid, playBtn, playMiniBtn,
  stopBtn, loopBtn, loopRegionEl,
  multitrack, currentJobId, trackIndex, totalDuration, loopEnabled,
  loopStart, loopEnd,
  masterVolume, masterFader, mixerState,
  setMultitrack, setCurrentJobId, setTrackIndex, setTotalDuration,
  setLoopEnabled, setLoopStart, setLoopEnd, setMasterVolume,
  setWaveZoom, waveScroll, dlAllStemsBtn, dlMixBtn,
} from "./state.js";
import {
  loadMixIntoState, resetMixerState, refreshMixerVisuals,
  setLaneControlsEnabled, ensureMixerStateDefaults, applyMix,
  renderRealMiniWave,
} from "./mixer.js";
import { renderMixerRow } from "./mixer.js";
import {
  buildRuler, updatePlayheadMarker, updateLoopRegionVisual,
  applyWaveZoom, buildPresenceRuler, updateFooterTimes,
  updatePresencePlayhead,
} from "./transport.js";

// Rows for stems the job did not return are hidden (e.g. a stem the
// separator failed to produce).
const _STEM_ROW_SELECTORS = [
  ".mixer-column .lane-header[data-stem]",
  ".stem-list span[data-stem]",
  ".energy-row[data-stem]",
  ".presence-bars i[data-stem]",
  ".presence-labels span",
  ".stem-waveform-row[data-stem]",
];

function applyStemSelectionFilter(presentNames) {
  const visibleTrackCount = Math.max(1, presentNames.size || STEM_NAMES.length);
  document.querySelector(".app")?.style.setProperty("--visible-track-count", String(visibleTrackCount));
  for (const sel of _STEM_ROW_SELECTORS) {
    for (const el of document.querySelectorAll(sel)) {
      const stem = el.dataset.stem
        || el.classList[0];  // .presence-labels span has no data-stem, use class
      el.classList.toggle("hidden", !presentNames.has(stem));
    }
  }
}

function clearStemSelectionFilter() {
  document.querySelector(".app")?.style.setProperty("--visible-track-count", String(STEM_NAMES.length));
  for (const sel of _STEM_ROW_SELECTORS) {
    for (const el of document.querySelectorAll(sel)) {
      el.classList.remove("hidden");
    }
  }
}

// Reset the analysis cards (key, scale, confidence ring, loudness)
// between songs so a re-import doesn't flash the previous song's
// numbers before the new ones arrive via SSE.
function resetAnalysisCards() {
  const summaryKey = document.getElementById("summary-key");
  const summaryBpm = document.getElementById("summary-bpm");
  const summaryScale = document.getElementById("summary-scale");
  const summaryConfidence = document.getElementById("summary-confidence");
  const summaryConfidenceLabel = document.getElementById("summary-confidence-label");
  const loudnessCard = document.getElementById("loudness-card");
  if (summaryKey) summaryKey.textContent = "—";
  if (summaryBpm) summaryBpm.innerHTML = "— <small>BPM</small>";
  if (summaryScale) summaryScale.textContent = "";
  if (summaryConfidence) {
    summaryConfidence.textContent = "";
    summaryConfidence.style.removeProperty("--confidence-pct");
    summaryConfidence.classList.add("hidden");
  }
  if (summaryConfidenceLabel) summaryConfidenceLabel.classList.add("hidden");
  if (loudnessCard) loudnessCard.classList.add("hidden");
}

function renderPlaceholderTracks() {
  multitrackContainer.innerHTML = "";
  for (const name of STEM_NAMES) {
    const ph = document.createElement("div");
    ph.className = "lane-placeholder";
    ph.dataset.stem = name;
    ph.style.setProperty("--lane-color", STEM_COLORS[name] || "#a0a0a0");
    multitrackContainer.appendChild(ph);
  }
}

const OVERVIEW_WAVE_POINTS = 1500;
const STEM_VU_FPS = 30;
let visualRenderToken = 0;
let stemVuRafId = null;

// ─── Master clock sync ───
//
// The multitrack bundle uses a private WebAudioPlayer per track. Each
// player's `_play()` calls `bufferNode.start(audioContext.currentTime,
// playedDuration)` -- which captures `audioContext.currentTime` AT THAT
// LINE OF JS. When the bundle iterates all tracks and calls play()
// sequentially, each captures a slightly later instant, so the tracks
// start a few ms apart. After several seeks that flam compounds into
// audible desync.
//
// The fix bypasses the per-track play sequence and instead schedules
// every track's bufferNode at the SAME future audio-clock time. The
// shared audio context guarantees sample-accurate alignment from there.
//
// Implementation reaches into WebAudioPlayer's properties (audioContext,
// buffer, gainNode, bufferNode, paused, playedDuration, playStartTime).
// These are non-private and stable across the bundle versions we ship.

const RESUME_LOOKAHEAD = 0.04;   // 40 ms — covers worst-case task latency
let _masterClockCleanup = null;
let _activeMt = null;
let _activeEls = null;
let _loopWrapTimerId = null;

function _mediaEl(ws) {
  try { return ws?.getMediaElement?.() ?? null; } catch { return null; }
}

function _isWebAudioPlayer(el) {
  // Duck-type the bundle's WebAudioPlayer (the only path that gives us
  // sample-accurate scheduling). HTMLMediaElement-backed media won't
  // have these fields set up.
  return !!(el && el.audioContext && el.buffer && el.gainNode);
}

function _patchCurrentTime(els) {
  // bufferNode.start(t0, ...) schedules audio to begin at t0 = now + 40 ms.
  // Until t0 actually arrives, ctx.currentTime - playStartTime is negative,
  // and the bundle's `currentTime` getter returns playedDuration - 40 ms.
  // That makes the playhead jump backwards on every play/seek. Clamp the
  // getter to never report a value below playedDuration.
  for (const el of els) {
    const proto = Object.getPrototypeOf(el);
    const desc = Object.getOwnPropertyDescriptor(proto, "currentTime");
    if (!desc?.get) continue;
    const origGet = desc.get;
    Object.defineProperty(el, "currentTime", {
      configurable: true,
      get() {
        const v = origGet.call(this);
        return v < this.playedDuration ? this.playedDuration : v;
      },
    });
  }
}

function _unpatchCurrentTime(els) {
  for (const el of els) {
    try { delete el.currentTime; } catch { /* ignore */ }
  }
}

function _atomicPauseAll(els) {
  // Stop every active bufferNode in the same JS task and update each
  // player's playedDuration to reflect "where it actually was". Math.max
  // guards against the lookahead window: if we pause within 40 ms of a
  // resume, ctx.currentTime - playStartTime is negative and would
  // *decrease* playedDuration, restarting before the user's intent.
  if (!els.length) return;
  const ctx = els[0].audioContext;
  const now = ctx?.currentTime ?? 0;
  for (const el of els) {
    if (el.paused) continue;
    el.paused = true;
    try { el.bufferNode?.stop(); } catch { /* ignore */ }
    el.playedDuration += Math.max(0, now - el.playStartTime);
  }
}

function _atomicResumeAll(els) {
  // Schedule every track's new bufferNode at the SAME `t0`. Because all
  // tracks share one AudioContext (the multitrack constructor creates
  // exactly one), starting all bufferNodes at t0 means each plays its
  // first sample at the same audio clock instant. Result: sample-
  // accurate sync, regardless of how long the JS scheduling loop took.
  if (!els.length) return;
  const ctx = els[0].audioContext;
  const t0 = ctx.currentTime + RESUME_LOOKAHEAD;
  for (const el of els) {
    if (!el.paused) continue;
    el.paused = false;
    try { el.bufferNode?.disconnect(); } catch { /* ignore */ }
    const node = ctx.createBufferSource();
    node.buffer = el.buffer;
    node.connect(el.gainNode);
    if (el.playedDuration >= el.duration) el.playedDuration = 0;
    // Mirror the bundle's natural-end lifecycle: when a track plays through
    // to its full duration the bufferNode emits `ended`, and the bundle
    // expects pause + emit("ended") to fire so transport state updates. Our
    // re-created nodes need the same handler — without it, the play button
    // stays stuck in "playing" past the end of the song.
    node.onended = () => {
      if (el.bufferNode !== node || el.paused) return;
      const live = el.playedDuration + el.audioContext.currentTime - el.playStartTime;
      if (live < el.duration - 0.01) return;  // user-initiated stop, not natural end
      el.paused = true;
      el.playedDuration = el.duration;
      try { el.emit?.("pause"); } catch { /* ignore */ }
      try { el.emit?.("finish"); } catch { /* ignore */ }
    };
    node.start(t0, el.playedDuration);
    el.bufferNode = node;
    el.playStartTime = t0;
    // Re-emit the bundle's "play" event so wavesurfer's UI listeners
    // (the play-button class toggle in wireUpAudio) still update.
    try { el.emit?.("play"); } catch { /* ignore */ }
  }
}

function _clearLoopWrap() {
  if (_loopWrapTimerId !== null) {
    clearTimeout(_loopWrapTimerId);
    _loopWrapTimerId = null;
  }
}

// Pre-schedule the loop wrap on the audio clock so it doesn't have to wait
// for the next RAF "timeupdate" tick (which adds up to 16 ms of latency on
// top of the 40 ms resume-lookahead). We can't make the wrap itself sample-
// accurate without rebuilding the bufferSource pipeline, but firing the
// setTime() call exactly when audio reaches loopEnd minimises the audible
// tail.
function _scheduleLoopWrap() {
  _clearLoopWrap();
  const mt = _activeMt;
  const els = _activeEls;
  if (!mt || !els?.length) return;
  if (!loopEnabled || !mt.isPlaying?.()) return;
  if (!(loopEnd > loopStart)) return;

  const m = els[0];
  const ctx = m.audioContext;
  const elapsed = Math.max(0, ctx.currentTime - m.playStartTime);
  const livePos = m.playedDuration + elapsed;

  if (livePos >= loopEnd) {
    // Already past loopEnd — wrap immediately. Defer to a microtask so we
    // don't recurse into setTime from inside a state mutation.
    queueMicrotask(() => {
      if (loopEnabled && mt.isPlaying?.()) mt.setTime(loopStart);
    });
    return;
  }

  // Fire 1 ms early so the timer never undershoots into a "loopEnd has
  // already passed" state when it runs.
  const remainingMs = Math.max(0, (loopEnd - livePos) * 1000 - 1);
  _loopWrapTimerId = setTimeout(() => {
    _loopWrapTimerId = null;
    if (loopEnabled && mt.isPlaying?.()) mt.setTime(loopStart);
  }, remainingMs);
}

// Called from transport.js / main.js when loop bounds or enabled state
// change while playback is active. Re-runs the wrap scheduler so the timer
// reflects the new bounds without requiring a full setTime(currentTime)
// re-schedule (which would add an audible 40 ms gap on every keystroke).
export function rearmLoopWrap() {
  _scheduleLoopWrap();
}

function stopMasterClock() {
  _clearLoopWrap();
  if (_activeEls) _unpatchCurrentTime(_activeEls);
  if (_masterClockCleanup) {
    _masterClockCleanup();
    _masterClockCleanup = null;
  }
  _activeMt = null;
  _activeEls = null;
}

function startMasterClock(mt, wsArr) {
  stopMasterClock();
  if (!mt || !wsArr || wsArr.length < 2) return;

  // Collect WebAudioPlayer instances. If any track isn't WebAudioPlayer
  // (e.g. a future change to use HTMLAudio), bail out and let the bundle
  // handle it -- our fix only works with the Web Audio backend.
  // wsArr holds the stem tracks only: the bundle appends an invisible
  // timeline track whose short placeholder buffer "ends" at once — driving
  // it too left one player unpaused forever, so isPlaying() stayed true
  // after the song ended.
  const els = wsArr.map(_mediaEl).filter(Boolean);
  if (!els.length || !els.every(_isWebAudioPlayer)) return;

  _patchCurrentTime(els);
  _activeMt = mt;
  _activeEls = els;

  const originalSetTime = mt.setTime?.bind(mt);
  const originalPlay = mt.play?.bind(mt);
  const originalPause = mt.pause?.bind(mt);
  const originalGetCurrentTime = mt.getCurrentTime?.bind(mt);

  // The bundle's own position (mt.currentTime, its cursor) is normally moved
  // by its startSync() loop, which only runs from the play() replaced below.
  // Drive it from the master element instead, or getCurrentTime() and the
  // cursor stay at 0 while the audio plays.
  const syncBundlePosition = (t) => {
    mt.currentTime = t;
    mt.rendering?.updateCursor?.(t / (mt.maxDuration || 1), true);
  };
  const unsubscribeTime = wsArr[0].on?.("timeupdate", syncBundlePosition);
  mt.getCurrentTime = () => els[0].currentTime;
  // The Web Audio player emits no timeupdate while playing, and its play /
  // pause never reach the wavesurfer, so neither the transport UI
  // (wireUpAudio's handlers: time, playhead, play-button state) nor the
  // position above would follow. Tick them from the master element.
  let tickId = 0;
  let wasPlaying = false;
  const tick = () => {
    const playing = !els[0].paused;
    if (playing !== wasPlaying) {
      wasPlaying = playing;
      wsArr[0].emit?.(playing ? "play" : "pause");
    }
    if (playing) wsArr[0].emit?.("timeupdate", els[0].currentTime);
    tickId = requestAnimationFrame(tick);
  };
  tickId = requestAnimationFrame(tick);

  mt.setTime = (time) => {
    const wasPlaying = mt.isPlaying?.() ?? false;
    _clearLoopWrap();
    _atomicPauseAll(els);
    for (const el of els) el.playedDuration = time;
    syncBundlePosition(time);
    // Seeks while paused must refresh the transport too (stop-button state).
    wsArr[0].emit?.("seeking", time);
    if (wasPlaying) {
      _atomicResumeAll(els);
      _scheduleLoopWrap();
    }
  };

  mt.play = () => {
    // No-op when every track is already playing. Without this guard a stray
    // play() call (double-click, external integration) would tear down the
    // bufferNodes and reschedule, introducing an audible micro-gap.
    if (els.every((el) => !el.paused)) return;
    // Snap all to track 0 first, then schedule everything at the same t0.
    // Math.max keeps us above playedDuration during the lookahead window.
    const live = els[0].playedDuration
      + Math.max(0, els[0].audioContext.currentTime - els[0].playStartTime);
    const t = els[0].paused ? els[0].playedDuration : live;
    for (const el of els) el.playedDuration = t;
    // Mark all paused so _atomicResumeAll will start them.
    for (const el of els) {
      if (!el.paused) {
        el.paused = true;
        try { el.bufferNode?.stop(); } catch { /* ignore */ }
      }
    }
    _atomicResumeAll(els);
    _scheduleLoopWrap();
  };

  mt.pause = () => {
    _clearLoopWrap();
    _atomicPauseAll(els);
    // Notify the wavesurfer event listeners so the UI play/pause button
    // updates correctly. The bundle's own pause() emits these per track.
    for (const el of els) {
      try { el.emit?.("pause"); } catch { /* ignore */ }
    }
  };

  _masterClockCleanup = () => {
    cancelAnimationFrame(tickId);
    unsubscribeTime?.();
    if (originalGetCurrentTime) mt.getCurrentTime = originalGetCurrentTime;
    if (originalSetTime) mt.setTime = originalSetTime;
    if (originalPlay) mt.play = originalPlay;
    if (originalPause) mt.pause = originalPause;
  };
}

function isAudioBufferLike(value) {
  return value && typeof value.getChannelData === "function";
}

function clearOverviewWaveforms() {
  document.querySelector(".stem-waveform-layer")?.remove();
}

function resetStemMeters() {
  for (const meter of document.querySelectorAll(".mini-meter")) {
    meter.style.setProperty("--vu-scale", "0");
    meter.style.setProperty("--vu-peak-pct", "0");
    meter.style.setProperty("--vu-peak-opacity", "0");
  }
  for (const laneVu of mixerEl.querySelectorAll(".lane-vu")) {
    laneVu.style.setProperty("--vu-level", "0%");
    laneVu.style.setProperty("--vu-peak", "0%");
  }
}

function stopStemVuLoop() {
  if (stemVuRafId) {
    cancelAnimationFrame(stemVuRafId);
    stemVuRafId = null;
  }
  resetStemMeters();
}

function ensureOverviewWaveformLayer() {
  let layer = document.querySelector(".stem-waveform-layer");
  if (!layer) {
    layer = document.createElement("div");
    layer.className = "stem-waveform-layer";
    multitrackContainer.parentElement?.appendChild(layer);
  }
  return layer;
}

// Standard DAW-style waveform: track min and max raw sample values per
// pixel column. The signed peaks let us render the natural mirror-
// symmetric shape (top edge follows max, bottom follows min) and keeps
// transient detail that an RMS envelope would smooth away.
function bufferMinMaxPeaks(audioBuffer, count) {
  const ch = audioBuffer.getChannelData(0);
  const binSize = Math.max(1, Math.floor(ch.length / count));
  const peaks = new Array(count);
  for (let i = 0; i < count; i++) {
    const start = i * binSize;
    const end = i === count - 1 ? ch.length : Math.min(ch.length, start + binSize);
    let mn = 0;
    let mx = 0;
    for (let j = start; j < end; j++) {
      const v = ch[j];
      if (v > mx) mx = v;
      else if (v < mn) mn = v;
    }
    peaks[i] = [mn, mx];
  }
  return peaks;
}

function minMaxWaveformPath(peaks, norm) {
  const n = peaks.length;
  const top = new Array(n);
  const bottom = new Array(n);
  for (let i = 0; i < n; i++) {
    const x = ((i / (n - 1)) * 100).toFixed(3);
    const mx = Math.min(1, peaks[i][1] * norm);
    const mn = Math.max(-1, peaks[i][0] * norm);
    top[i] = `${i === 0 ? "M" : "L"}${x} ${(24 - mx * 21).toFixed(3)}`;
    bottom[n - 1 - i] = `L${x} ${(24 - mn * 21).toFixed(3)}`;
  }
  return `${top.join(" ")} ${bottom.join(" ")} Z`;
}

function renderOverviewWaveformPath(stemName, peaks, norm, color) {
  const layer = ensureOverviewWaveformLayer();
  let row = layer.querySelector(`[data-stem="${stemName}"]`);
  if (!row) {
    row = document.createElement("div");
    row.className = "stem-waveform-row";
    row.dataset.stem = stemName;
    layer.appendChild(row);
  }
  row.style.setProperty("--stem-color", color);
  row.style.order = String(STEM_NAMES.indexOf(stemName));
  row.innerHTML = `
    <svg class="stem-waveform-svg" viewBox="0 0 100 48" preserveAspectRatio="none" aria-hidden="true">
      <path d="${minMaxWaveformPath(peaks, norm)}"></path>
    </svg>
  `;
}

// Normalize all stems to a single shared max so the overview waveforms
// preserve real amplitude relationships (drums tall, piano short),
// matching what a DAW shows. Per-stem normalization made every lane
// fill its row regardless of how loud the stem actually was.
function renderAllOverviewWaveforms(stems, decodedMap) {
  const peaksByStem = new Map();
  let globalMax = 0;
  for (const stem of stems) {
    const buf = decodedMap.get(stem.name);
    if (!isAudioBufferLike(buf)) continue;
    const peaks = bufferMinMaxPeaks(buf, OVERVIEW_WAVE_POINTS);
    peaksByStem.set(stem.name, peaks);
    for (const [mn, mx] of peaks) {
      if (mx > globalMax) globalMax = mx;
      if (-mn > globalMax) globalMax = -mn;
    }
  }
  if (globalMax <= 0) return;
  const norm = 1 / globalMax;
  for (const stem of stems) {
    const peaks = peaksByStem.get(stem.name);
    if (!peaks) continue;
    const color = STEM_COLORS[stem.name] || "#a0a0a0";
    renderOverviewWaveformPath(stem.name, peaks, norm, color);
  }
}

function renderDecodedStemVisuals(stemName, audioBuffer, color) {
  if (!isAudioBufferLike(audioBuffer)) return;
  renderRealMiniWave(stemName, audioBuffer, color);
}

// Set the song-level "Stem Energy" panel from each stem's overall RMS.
// Without this baseline the bars sit at 0% until the user hits play
// (because the VU loop only writes per-frame during active playback) and
// look like static placeholders. Normalizing all stems to the loudest
// one's RMS gives a meaningful relative balance ("drums dominate, piano
// quiet"), which is what a DAW-style energy panel is supposed to show.
// Once playback starts, the VU loop's per-frame writes override these
// baseline values for real-time pulsing.
function renderStemEnergyBaseline(stems, decodedMap) {
  const rmsByStem = new Map();
  let maxRms = 0;
  for (const stem of stems) {
    const buf = decodedMap.get(stem.name);
    if (!isAudioBufferLike(buf)) continue;
    const ch = buf.getChannelData(0);
    if (!ch?.length) continue;
    let sum = 0;
    for (let i = 0; i < ch.length; i++) sum += ch[i] * ch[i];
    const rms = Math.sqrt(sum / ch.length);
    rmsByStem.set(stem.name, rms);
    if (rms > maxRms) maxRms = rms;
  }
  if (maxRms <= 0) return;
  for (const [name, rms] of rmsByStem) {
    const pct = Math.round((rms / maxRms) * 100);
    const row = document.querySelector(`.energy-row[data-stem="${name}"]`);
    if (!row) continue;
    const bar = row.querySelector("b");
    const txt = row.querySelector("em");
    if (bar) bar.style.setProperty("--v", `${pct}%`);
    if (txt) txt.textContent = `${pct}%`;
  }
}

function buildStemVuEnvelope(audioBuffer) {
  if (!isAudioBufferLike(audioBuffer)) return [];
  const ch = audioBuffer.getChannelData(0);
  const sampleRate = audioBuffer.sampleRate || 44100;
  const duration = audioBuffer.duration || (ch.length / sampleRate);
  const frameCount = Math.max(1, Math.ceil(duration * STEM_VU_FPS));
  const hop = Math.max(1, Math.floor(sampleRate / STEM_VU_FPS));
  const win = Math.max(1, Math.floor(sampleRate * 0.045));
  const env = new Float32Array(frameCount);
  let max = 0;
  for (let i = 0; i < frameCount; i++) {
    const center = Math.min(ch.length - 1, i * hop);
    const start = Math.max(0, center - Math.floor(win / 2));
    const end = Math.min(ch.length, start + win);
    let sum = 0;
    let peak = 0;
    for (let j = start; j < end; j++) {
      const v = Math.abs(ch[j]);
      sum += v * v;
      if (v > peak) peak = v;
    }
    const rms = Math.sqrt(sum / Math.max(1, end - start));
    const level = rms * 0.78 + peak * 0.22;
    env[i] = level;
    if (level > max) max = level;
  }
  if (max <= 0) return env;
  for (let i = 0; i < env.length; i++) {
    env[i] = Math.min(1, Math.sqrt(env[i] / max));
  }
  return env;
}

function stemVuGain(stemName) {
  const state = mixerState[stemName];
  if (!state) return 0;
  const anySolo = STEM_NAMES.some((name) => trackIndex[name] !== undefined && mixerState[name]?.soloed);
  if (state.muted || (anySolo && !state.soloed)) return 0;
  return Math.max(0, state.volume);
}

function startStemVuLoop(stems, decodedMap, token) {
  stopStemVuLoop();
  const meters = stems.map((stem) => ({
    name: stem.name,
    env: buildStemVuEnvelope(decodedMap.get(stem.name)),
    miniMeterEl: document.querySelector(`.stem-list [data-stem="${stem.name}"] .mini-meter`),
    vuEl: mixerEl.querySelector(`.lane-vu[data-stem="${stem.name}"]`),
    peak: 0,
    peakHold: 0,
    holdFrames: 0,
    lastPeakPct: -1,
    lastHoldPct: -1,
    lastLevelPct: -1,
  })).filter((m) => m.env.length && (m.miniMeterEl || m.vuEl));

  if (!meters.length) return;
  const tick = () => {
    if (token !== visualRenderToken || !multitrack) return;
    const playing = multitrack.isPlaying?.() ?? false;
    const time = multitrack.getCurrentTime?.() ?? 0;
    for (const m of meters) {
      const idx = Math.max(0, Math.min(m.env.length - 1, Math.floor(time * STEM_VU_FPS)));
      const gain = stemVuGain(m.name);
      const input = playing && gain > 0 ? Math.min(1, m.env[idx] * gain) : 0;
      if (gain <= 0) {
        m.peak = 0;
        m.peakHold = 0;
        m.holdFrames = 0;
      }
      const nextPeak = input > m.peak ? input : Math.max(0, m.peak - 0.018);
      m.peak = nextPeak;

      if (input > m.peakHold) {
        m.peakHold = input;
        m.holdFrames = 28;
      } else if (m.holdFrames > 0) {
        m.holdFrames -= 1;
      } else {
        m.peakHold = Math.max(0, m.peakHold - 0.025);
      }

      const lvlPct = Math.round(input * 100);
      const peakPct = Math.round(nextPeak * 100);
      const holdPct = Math.round(m.peakHold * 100);

      if (m.miniMeterEl) {
        if (peakPct !== m.lastPeakPct) {
          m.miniMeterEl.style.setProperty("--vu-scale", nextPeak.toFixed(3));
        }
        if (holdPct !== m.lastHoldPct) {
          m.miniMeterEl.style.setProperty("--vu-peak-pct", String(holdPct));
          m.miniMeterEl.style.setProperty("--vu-peak-opacity", m.peakHold > 0.04 ? "1" : "0");
        }
      }
      if (m.vuEl) {
        if (lvlPct !== m.lastLevelPct) m.vuEl.style.setProperty("--vu-level", `${lvlPct}%`);
        if (holdPct !== m.lastHoldPct) m.vuEl.style.setProperty("--vu-peak", `${holdPct}%`);
      }
      m.lastLevelPct = lvlPct;
      m.lastPeakPct = peakPct;
      m.lastHoldPct = holdPct;
    }
    stemVuRafId = requestAnimationFrame(tick);
  };
  stemVuRafId = requestAnimationFrame(tick);
}

// Visuals reuse audio the multitrack bundle has already decoded instead of
// fetching and decoding every stem a second time: the full-rate playback
// buffer of the Web Audio player (always used — see the patch note at the top
// of vendor/multitrack.js), else wavesurfer's own decode if a track ever plays
// through <audio>, which has no buffer.
function decodedForVisuals(ws) {
  if (!ws) return Promise.resolve(null);
  const full = _mediaEl(ws)?.buffer;
  if (isAudioBufferLike(full)) return Promise.resolve(full);
  const decoded = ws.getDecodedData?.();
  if (isAudioBufferLike(decoded)) return Promise.resolve(decoded);
  return new Promise((resolve) => {
    ws.once("decode", () => resolve(ws.getDecodedData?.() ?? null));
    ws.once("error", () => resolve(null));
  });
}

async function renderAllDecodedVisuals(stems, wsArr, token) {
  const buffers = await Promise.all(stems.map((_, i) => decodedForVisuals(wsArr[i])));
  if (token !== visualRenderToken) return;
  clearOverviewWaveforms();
  const decoded = new Map();
  stems.forEach((stem, i) => {
    const buf = buffers[i];
    if (!isAudioBufferLike(buf)) {
      console.warn(`[visuals] ${stem.name}: no decoded audio`);
      return;
    }
    decoded.set(stem.name, buf);
    renderDecodedStemVisuals(stem.name, buf, STEM_COLORS[stem.name] || "#a0a0a0");
  });
  renderAllOverviewWaveforms(stems, decoded);
  renderStemEnergyBaseline(stems, decoded);
  startStemVuLoop(stems, decoded, token);
}

export function destroyPlayer() {
  document.querySelector(".app")?.classList.remove("is-import");
  stopStemVuLoop();
  stopMasterClock();
  if (multitrack) {
    multitrack.destroy();
    setMultitrack(null);
  }
  renderPlaceholderTracks();
  clearOverviewWaveforms();
  for (const row of mixerEl.querySelectorAll(".lane-header")) {
    const dl = row.querySelector(".lane-dl");
    if (dl) {
      dl.href = "#";
      dl.removeAttribute("download");
    }
  }
  resetMixerState();
  refreshMixerVisuals();
  setLaneControlsEnabled(false);
  // Reset static rows, then keep the pre-import shell to extractable stems
  // only. wireUpAudio will re-apply the exact returned-track set.
  clearStemSelectionFilter();
  applyStemSelectionFilter(new Set(STEM_NAMES));
  npThumb.classList.remove("loaded");
  npThumb.removeAttribute("src");
  if (dlAllStemsBtn) { dlAllStemsBtn.removeAttribute("href"); dlAllStemsBtn.classList.add("hidden"); }
  if (dlMixBtn) dlMixBtn.classList.add("hidden");

  rulerTime.innerHTML = '<div class="playhead-marker" aria-hidden="true"><svg viewBox="0 0 10 10" width="10" height="10"><polygon points="0,0 10,0 5,8" fill="#e54e4e"></polygon></svg></div>';
  wavesGrid.innerHTML = "";

  titleEl.textContent = "";
  bpmChip.textContent = "\u2014 BPM";
  keyChip.textContent = "\u2014 \u2014";
  stemsChip.textContent = "\u2014 Stems";
  timeEl.textContent = "00:00 / 00:00";
  resetAnalysisCards();

  for (const row of document.querySelectorAll(".energy-row")) {
    const bar = row.querySelector("b");
    const txt = row.querySelector("em");
    if (bar) bar.style.setProperty("--v", "0%");
    if (txt) txt.textContent = "0%";
  }
  setTotalDuration(0);
  setLoopEnabled(false);
  setLoopStart(0);
  setLoopEnd(0);
  setMasterVolume(0.5);
  setTrackIndex({});
  setWaveZoom(1);
  applyWaveZoom();
  buildPresenceRuler(0);
  updateFooterTimes(0);
  updatePresencePlayhead(0);
  if (waveScroll) waveScroll.scrollLeft = 0;
  loopBtn.classList.remove("active");
  playBtn.classList.remove("playing");
  stopBtn.classList.remove("stopped");
  loopRegionEl.classList.add("hidden");
}

export function renderEmptyShell() {
  document.querySelector(".app")?.classList.remove("is-import");
  stopStemVuLoop();
  ensureMixerStateDefaults();
  mixerEl.innerHTML = "";
  for (const name of STEM_NAMES) {
    const { row } = renderMixerRow({ name, url: "#" });
    mixerEl.appendChild(row);
  }
  applyStemSelectionFilter(new Set(STEM_NAMES));
  titleEl.textContent = "Ready to import a track";
  bpmChip.textContent = "\u2014 BPM";
  keyChip.textContent = "\u2014 \u2014";
  stemsChip.textContent = "\u2014 Stems";
  timeEl.textContent = "00:00 / 00:00";
  resetAnalysisCards();
  renderPlaceholderTracks();
  clearOverviewWaveforms();
  setLaneControlsEnabled(false);
}

export function wireUpAudio(jobId, stems, duration, thumbnail) {
  document.querySelector(".app")?.classList.remove("is-import");
  visualRenderToken += 1;
  const token = visualRenderToken;
  setCurrentJobId(jobId);
  setTotalDuration(duration || 0);
  loadMixIntoState(jobId);
  refreshMixerVisuals();
  setLaneControlsEnabled(true);

  // Hide rows for any stem the backend did not return.
  applyStemSelectionFilter(new Set(stems.map((s) => s.name)));

  for (const stem of stems) {
    const row = mixerEl.querySelector(`.lane-header[data-stem="${stem.name}"]`);
    if (!row) continue;
    const dl = row.querySelector(".lane-dl");
    if (dl) {
      dl.href = stem.url;
      dl.download = `${stem.name}.wav`;
    }
  }

  stemsChip.textContent = `${stems.length} Stems`;
  if (dlAllStemsBtn) {
    dlAllStemsBtn.href = `/api/jobs/${jobId}/stems.zip`;
    dlAllStemsBtn.download = "";
    dlAllStemsBtn.classList.remove("hidden");
  }

  if (dlMixBtn) {
    dlMixBtn.classList.remove("hidden");
    dlMixBtn.onclick = () => {
      const allNames = Object.keys(trackIndex);
      const anySolo = allNames.some((n) => mixerState[n]?.soloed);
      const active = allNames.filter((n) => {
        const s = mixerState[n];
        if (!s) return false;
        if (s.muted) return false;
        if (anySolo && !s.soloed) return false;
        return (s.volume ?? 1) > 0;
      });
      if (!active.length) return;
      const stemParam = active.join(",");
      const volParam = active.map((n) => (mixerState[n]?.volume ?? 1).toFixed(4)).join(",");
      const pitchParam = active.map((n) => (mixerState[n]?.pitch ?? 0)).join(",");
      const url = `/api/jobs/${jobId}/remix.wav?stems=${encodeURIComponent(stemParam)}&volumes=${encodeURIComponent(volParam)}&pitches=${encodeURIComponent(pitchParam)}`;
      const a = document.createElement("a");
      a.href = url;
      a.download = "";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    };
  }

  if (thumbnail) {
    npThumb.onload = () => npThumb.classList.add("loaded");
    npThumb.onerror = () => npThumb.classList.remove("loaded");
    npThumb.src = thumbnail;
  }

  clearOverviewWaveforms();

  // Disable play until canplay fires. Without the master clock installed,
  // the bundle's original mt.play() iterates audios[i]._play() sequentially
  // and each call captures audioContext.currentTime at a slightly different
  // instant — exactly the desync this whole subsystem exists to prevent.
  if (playBtn) playBtn.disabled = true;
  if (playMiniBtn) playMiniBtn.disabled = true;

  setTrackIndex(Object.fromEntries(stems.map((s, i) => [s.name, i])));
  multitrackContainer.innerHTML = "";
  const mt = Multitrack.create(
    stems.map((s, i) => ({
      id: i,
      url: s.url,
      draggable: false,
      startPosition: 0,
      volume: 1,
      options: {
        waveColor: STEM_COLORS[s.name] || "#a0a0a0",
        progressColor: PROGRESS_COLOR,
        height: 48,
        cursorWidth: 0,
        // Disable per-waveform click-to-seek. The bundle would
        // otherwise let a click on (say) the drums waveform call
        // setTime on JUST that wavesurfer's media — which reschedules
        // its bufferNode at a NEW t0 and immediately desyncs from the
        // others. All seeks must go through mt.setTime so the master
        // clock can resume every track at a shared t0.
        interact: false,
      },
    })),
    {
      container: multitrackContainer,
      // 0 = fit waveforms to the container width. Any positive value
      // makes the bundle's internal div wider than the visible area
      // (so it scrolls horizontally), while our ruler ticks, playhead
      // marker, and loop-region all render relative to the visible
      // waves-column width — they go out of sync the moment the inner
      // div scrolls. Fitting to view keeps the three perfectly aligned.
      minPxPerSec: 0,
      rightButtonDrag: false,
      cursorWidth: 1.5,
      cursorColor: "#e54e4e",
      trackBackground: "#050505",
      trackBorderColor: "#2a2a2a",
    },
  );
  setMultitrack(mt);

  // Stop button glows iff transport is paused AND at the "start" (0,
  // or loopStart if loop is on). Centralised here so manual seeks via
  // the ruler also update the visual without extra plumbing.
  const STOP_TOLERANCE_SEC = 0.15;
  const updateStopVisual = () => {
    const t = mt.getCurrentTime?.() ?? 0;
    const startPos = loopEnabled ? loopStart : 0;
    const atStart = Math.abs(t - startPos) < STOP_TOLERANCE_SEC;
    const stopped = !mt.isPlaying() && atStart;
    stopBtn.classList.toggle("stopped", stopped);
  };

  mt.once("canplay", () => {
    if (!totalDuration) setTotalDuration(mt.getDuration() || 0);
    timeEl.textContent = `00:00 / ${fmtTime(totalDuration)}`;
    buildRuler(totalDuration);
    buildPresenceRuler(totalDuration);
    updateFooterTimes(0);
    updatePresencePlayhead(0);
    setMasterVolume(masterFader ? parseFloat(masterFader.value) : 1);
    applyMix();
    setLoopStart(totalDuration * LOOP_DEFAULT_START_FRAC);
    setLoopEnd(totalDuration * LOOP_DEFAULT_END_FRAC);
    applyWaveZoom();

    // CRITICAL: the Multitrack class itself does NOT emit play / pause /
    // timeupdate / seeking — those fire on the individual wavesurfer
    // instances. We pick wavesurfers[0] as the master clock since all
    // stems are kept in sync by the bundle's startSync() loop.
    const wsArr = mt.wavesurfers || mt._wavesurfers;
    const ws = wsArr?.[0];
    if (!ws) return;
    // Stem tracks only (ids 0..n-1), not the bundle's timeline track.
    const stemWs = wsArr.filter((_, i) => mt.tracks?.[i]?.id < stems.length);
    startMasterClock(mt, stemWs);
    renderAllDecodedVisuals(stems, wsArr, token);
    if (playBtn) playBtn.disabled = false;
    if (playMiniBtn) playMiniBtn.disabled = false;

    ws.on("timeupdate", (t) => {
      timeEl.textContent = `${fmtTime(t)} / ${fmtTime(totalDuration)}`;
      updatePlayheadMarker(t);
      updateFooterTimes(t);
      updatePresencePlayhead(t);
      updateStopVisual();
      // Defensive fallback: the audio-clock-pinned scheduleLoopWrap timer
      // is the primary mechanism, but if a tab gets backgrounded the timer
      // may fire late. Catch any drift past loopEnd here.
      if (loopEnabled && totalDuration > 0 && t >= loopEnd) {
        mt.setTime(loopStart);
      }
    });
    ws.on("play", () => {
      playBtn.classList.add("playing");
      stopBtn.classList.remove("stopped");
    });
    ws.on("pause", () => {
      playBtn.classList.remove("playing");
      updateStopVisual();
    });
    ws.on("seeking", updateStopVisual);
  });
}
