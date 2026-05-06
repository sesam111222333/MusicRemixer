const { invoke } = window.__TAURI__.core;

const statusEl = document.getElementById("status");
const detailsEl = document.getElementById("details");
const retryBtn = document.getElementById("retry");
const steps = [...document.querySelectorAll("[data-step]")];

function setStep(name, state) {
  const el = steps.find((item) => item.dataset.step === name);
  if (!el) return;
  el.classList.remove("active", "done", "error");
  if (state) el.classList.add(state);
}

function setStatus(message) {
  statusEl.textContent = message;
}

function showError(error) {
  setStatus("Setup could not complete.");
  detailsEl.textContent = String(error);
  detailsEl.classList.remove("hidden");
  retryBtn.classList.remove("hidden");
}

async function runSetup() {
  detailsEl.classList.add("hidden");
  retryBtn.classList.add("hidden");
  for (const step of steps) step.classList.remove("active", "done", "error");

  try {
    setStep("runtime", "active");
    setStatus("Checking bundled runtime...");
    const runtime = await invoke("probe_runtime");
    setStep("runtime", "done");

    setStep("workspace", "active");
    setStatus("Creating portable workspace...");
    await invoke("ensure_workspace");
    setStep("workspace", "done");

    setStep("ffmpeg", runtime.ffmpegReady ? "done" : "active");
    setStatus(runtime.ffmpegReady ? "FFmpeg is ready." : "Preparing FFmpeg...");
    const assets = await invoke("ensure_external_assets");
    if (!assets.ffmpegReady) {
      throw new Error("FFmpeg setup did not complete.");
    }
    setStep("ffmpeg", "done");

    setStep("model", "active");
    setStatus(assets.modelReady ? "AI separation model is ready." : "AI separation model will download during first separation.");
    setStep("model", "done");

    setStep("backend", "active");
    setStatus("Starting StemDeck backend...");
    const backend = await invoke("start_backend");
    setStep("backend", "done");

    setStatus("Opening StemDeck...");
    window.location.replace(backend.url);
  } catch (error) {
    const active = steps.find((step) => step.classList.contains("active"));
    if (active) active.classList.add("error");
    showError(error);
  }
}

retryBtn.addEventListener("click", runSetup);
runSetup();
