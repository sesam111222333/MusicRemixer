const HISTORY_KEY = "stemdeck:history";
const MAX_HISTORY = 20;

function loadHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    if (raw) return JSON.parse(raw);
  } catch { /* ignore */ }
  return [];
}

function persistHistory(history) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
  } catch { /* ignore */ }
}

export function saveToHistory(state) {
  if (!state.job_id || !state.title) return;
  const entry = {
    jobId: state.job_id,
    title: state.title,
    thumbnail: state.thumbnail || null,
    bpm: state.bpm || null,
    key: state.key || null,
    duration: state.duration || 0,
    timestamp: Date.now(),
  };
  let history = loadHistory();
  history = history.filter((h) => h.jobId !== entry.jobId);
  history.unshift(entry);
  history = history.slice(0, MAX_HISTORY);
  persistHistory(history);
  renderHistoryPanel(history);
}

function fmtDuration(secs) {
  if (!secs) return "";
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function fmtRelTime(ts) {
  const diff = Date.now() - ts;
  const m = Math.floor(diff / 60000);
  const h = Math.floor(diff / 3600000);
  const d = Math.floor(diff / 86400000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  if (h < 24) return `${h}h ago`;
  return `${d}d ago`;
}

let _onReopen = null;

async function reopenJob(entry, itemEl) {
  if (!_onReopen) return;
  itemEl.disabled = true;
  itemEl.classList.add("loading");
  try {
    const r = await fetch(`/api/jobs/${entry.jobId}`);
    if (!r.ok) {
      if (r.status === 404) {
        const history = loadHistory().filter((h) => h.jobId !== entry.jobId);
        persistHistory(history);
        renderHistoryPanel(history);
      }
      return;
    }
    const state = await r.json();
    if (state.status !== "done") return;
    _onReopen(state);
  } catch (err) {
    console.warn("[history] reopen failed:", err);
  } finally {
    itemEl.disabled = false;
    itemEl.classList.remove("loading");
  }
}

function renderHistoryPanel(history) {
  const panel = document.getElementById("history-panel");
  if (!panel) return;
  const list = document.getElementById("history-list");
  if (!list) return;

  if (!history.length) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");

  list.innerHTML = "";
  for (const entry of history) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "history-item";
    item.dataset.jobId = entry.jobId;
    item.setAttribute("aria-label", `Reload: ${entry.title}`);

    const thumb = document.createElement("div");
    thumb.className = "history-thumb";
    if (entry.thumbnail) {
      const img = document.createElement("img");
      img.src = entry.thumbnail;
      img.alt = "";
      img.referrerPolicy = "no-referrer";
      img.loading = "lazy";
      thumb.appendChild(img);
    } else {
      thumb.innerHTML = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg>';
    }

    const info = document.createElement("div");
    info.className = "history-info";

    const title = document.createElement("div");
    title.className = "history-title";
    title.textContent = entry.title;

    const chips = document.createElement("div");
    chips.className = "history-chips";
    const parts = [];
    if (entry.bpm) parts.push(`<span>${entry.bpm} BPM</span>`);
    if (entry.key) parts.push(`<span>${entry.key}</span>`);
    const dur = fmtDuration(entry.duration);
    if (dur) parts.push(`<span>${dur}</span>`);
    chips.innerHTML = parts.join("");

    const timeEl = document.createElement("div");
    timeEl.className = "history-time";
    timeEl.textContent = fmtRelTime(entry.timestamp);

    info.append(title, chips, timeEl);
    item.append(thumb, info);
    item.addEventListener("click", () => reopenJob(entry, item));
    list.appendChild(item);
  }
}

export function initHistoryPanel(onReopen) {
  _onReopen = onReopen;

  const toggle = document.getElementById("history-toggle");
  const list = document.getElementById("history-list");
  if (toggle && list) {
    toggle.addEventListener("click", () => {
      const expanded = toggle.getAttribute("aria-expanded") !== "false";
      toggle.setAttribute("aria-expanded", String(!expanded));
      list.classList.toggle("collapsed", expanded);
    });
  }

  renderHistoryPanel(loadHistory());
}
