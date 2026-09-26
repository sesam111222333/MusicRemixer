// "Recent" list of finished jobs, kept per browser in localStorage. The
// server deletes jobs after its TTL, so the list is pruned against
// GET /api/jobs/{id} every time it is opened.
const HISTORY_KEY = "stemdeck:history";
const MAX_HISTORY = 20;

const wrap = document.getElementById("history-wrap");
const toggle = document.getElementById("history-toggle");
const panel = document.getElementById("history-panel");
const list = document.getElementById("history-list");
let onOpenJob = null;

function load() {
  try {
    const raw = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(raw) ? raw.filter((h) => h && typeof h.jobId === "string") : [];
  } catch {
    return [];
  }
}

function persist(history) {
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(history)); } catch { /* quota / private mode */ }
  wrap.classList.toggle("hidden", history.length === 0);
}

export function saveToHistory(state) {
  if (!state.job_id) return;
  const entry = {
    jobId: state.job_id,
    title: state.title || state.job_id,
    thumbnail: state.thumbnail || null,
    bpm: state.bpm || null,
    key: state.key || null,
    duration: state.duration || 0,
    timestamp: Date.now(),
  };
  persist([entry, ...load().filter((h) => h.jobId !== entry.jobId)].slice(0, MAX_HISTORY));
}

function fmtDuration(secs) {
  if (!secs) return "";
  return `${Math.floor(secs / 60)}:${String(Math.floor(secs % 60)).padStart(2, "0")}`;
}

function fmtRelTime(ts) {
  const m = Math.floor((Date.now() - ts) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h}h ago` : `${Math.floor(h / 24)}d ago`;
}

function render(history) {
  list.textContent = "";
  if (!history.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = "No recent tracks";
    list.append(empty);
    return;
  }
  for (const entry of history) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "history-item";
    item.setAttribute("role", "menuitem");

    const thumb = document.createElement("div");
    thumb.className = "history-thumb";
    if (entry.thumbnail) {
      const img = document.createElement("img");
      img.src = entry.thumbnail;
      img.alt = "";
      img.referrerPolicy = "no-referrer";
      img.loading = "lazy";
      thumb.append(img);
    }

    const info = document.createElement("div");
    info.className = "history-info";
    const title = document.createElement("div");
    title.className = "history-title";
    title.textContent = entry.title;
    const meta = document.createElement("div");
    meta.className = "history-chips";
    meta.textContent = [entry.bpm && `${entry.bpm} BPM`, entry.key, fmtDuration(entry.duration), fmtRelTime(entry.timestamp)]
      .filter(Boolean).join(" · ");
    info.append(title, meta);
    item.append(thumb, info);
    item.addEventListener("click", () => openEntry(entry, item));
    list.append(item);
  }
}

async function openEntry(entry, item) {
  item.disabled = true;
  try {
    const r = await fetch(`/api/jobs/${entry.jobId}`);
    if (r.status === 404) {
      persist(load().filter((h) => h.jobId !== entry.jobId));
      render(load());
      return;
    }
    if (!r.ok) return;
    const state = await r.json();
    if (state.status !== "done") return;
    setOpen(false);
    onOpenJob?.(state);
  } finally {
    item.disabled = false;
  }
}

// Drop entries the server no longer has (TTL sweep, deleted) before showing.
async function prune() {
  const history = load();
  const alive = await Promise.all(history.map(async (h) => {
    try { return (await fetch(`/api/jobs/${h.jobId}`)).status !== 404; } catch { return true; }
  }));
  const kept = history.filter((_, i) => alive[i]);
  if (kept.length !== history.length) persist(kept);
  return kept;
}

function setOpen(open) {
  panel.classList.toggle("hidden", !open);
  toggle.setAttribute("aria-expanded", String(open));
}

export function initHistoryPanel(openJob) {
  onOpenJob = openJob;
  wrap.classList.toggle("hidden", load().length === 0);
  toggle.addEventListener("click", async () => {
    if (!panel.classList.contains("hidden")) return setOpen(false);
    render(load());
    setOpen(true);
    render(await prune());
  });
  document.addEventListener("click", (e) => {
    if (!wrap.contains(e.target)) setOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setOpen(false);
  });
}
