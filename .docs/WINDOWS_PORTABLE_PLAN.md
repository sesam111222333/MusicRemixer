# Windows Portable App Plan

## Goal

Ship StemDeck on Windows as a portable desktop app:

```text
Download zip -> Extract folder -> Double-click StemDeck.exe
```

The user should not need to install Python, FFmpeg, uv, Docker, or open a browser manually.

This is explicitly **not** a Windows installer release. The public artifact
should be a `.zip` attached to GitHub Releases. After extraction, the folder
should be self-contained except for first-run downloads stored inside its own
`data/` directory.

## Recommended Distribution Shape

Use a Tauri desktop shell that launches the existing FastAPI backend locally and opens the UI in a native WebView window.

Assumptions for the alpha Windows build:

- Windows 11 target.
- Internet access is available during first launch.
- WebView2 is expected to already be present through Windows/Microsoft Edge.
- If WebView2 is missing, show a clear setup error with a link to Microsoft's WebView2 runtime page rather than bundling WebView2 in the zip.

```text
StemDeck-Windows-x64/
  StemDeck.exe
  resources/ or bundled Tauri assets
  backend/
    app/
    static/
    pyproject.toml or packaged backend files
  python/
    python.exe
    Lib/
    Scripts/
    site-packages/
  data/
    config.json
    cache/
    jobs/
    logs/
    models/
    ffmpeg/
```

## What To Bundle

Bundle the pieces that are too fragile for end users to install:

- Tauri shell
- Python runtime
- StemDeck backend and static UI
- Python packages required by the app:
  - FastAPI
  - Uvicorn
  - yt-dlp
  - Demucs
  - Torch
  - torchaudio
  - librosa
  - pyloudnorm
  - soundfile

This keeps startup reliable and avoids asking users to solve Python or Torch installation problems.

## Sprint 1: Windows Launcher Alpha Release

Goal: ship the first Windows alpha portable zip with a Tauri launcher, bundled Python runtime/dependencies, first-run setup for FFmpeg and Demucs model assets, and a working local StemDeck window.

Release target:

```text
StemDeck-alpha-windows-x64.zip
```

Release format:

- [ ] Publish only a portable `.zip` for the alpha.
- [ ] The extracted folder contains `StemDeck.exe`.
- [ ] Users launch the app by double-clicking `StemDeck.exe`.
- [ ] No MSI, NSIS, setup wizard, or system installer artifact.
- [ ] No registry writes, Start Menu shortcuts, services, or system-wide dependency installation.
- [ ] First-run downloads are stored inside the extracted folder, under `data/`.

Definition of done:

- A Windows 11 user can extract the zip and double-click `StemDeck.exe`.
- StemDeck opens as a desktop app, not a manual browser session.
- First-run setup downloads required external assets.
- The backend starts and stops with the Tauri app.
- A YouTube URL can be processed end-to-end after setup.
- The package includes third-party notices.
- Deleting the extracted folder removes the app and its generated state.

Current status:

- Backend portable-folder support is implemented.
- Backend health endpoints are implemented.
- A first Tauri launcher/setup scaffold exists under `desktop/`.
- Temporary `build/windows/` packaging scaffold was removed; final Windows packaging should be implemented as a Windows-friendly portable staging script.
- Backend tests pass locally.
- Rust/Tauri compile validation passes with Rust `1.95.0`.
- First-run FFmpeg setup is implemented for Windows and verifies `ffmpeg -version`.
- First-run Demucs model preparation is not implemented yet.
- A real Windows portable zip has not been produced or VM-tested yet.

### Backend Readiness

- [x] Add `GET /health` returning a small JSON payload with app name/version/status.
- [x] Add `STEMDECK_DATA_DIR` support for jobs, logs, cache, downloads, and runtime state.
- [x] Ensure the default dev behavior remains unchanged when `STEMDECK_DATA_DIR` is unset.
- [x] Add configurable FFmpeg lookup that prefers `<data>/ffmpeg/ffmpeg.exe` on Windows.
- [ ] Add clear backend startup errors for missing FFmpeg or model assets.
- [x] Ensure Demucs/Torch cache paths can be controlled with portable-folder env vars.
- [ ] Confirm yt-dlp, FFmpeg, Demucs, and analysis outputs all write under `data/`.
- [x] Add tests for `/health` and data-directory path resolution.

### Tauri Shell

- [x] Scaffold a Tauri app under `desktop/` or `build/tauri/`.
- [x] Configure Tauri app name, icon, window size, and dark background.
- [x] Build a small Rust command/module to resolve the portable app root.
- [x] Start the bundled Python backend as a child process.
- [x] Use a random available localhost port and pass it to the backend.
- [x] Poll `/health` before opening the main UI.
- [x] Load the StemDeck UI inside the Tauri WebView.
- [x] Hide backend console windows on Windows.
- [x] Kill the backend process when the Tauri app exits.
- [ ] Open external links, including GitHub issues, in the system browser.
- [ ] Show a clear WebView2 missing message if WebView2 is unavailable.
- [x] Compile and verify the Tauri launcher with Rust `1.88+`.

### First-Run Setup

- [x] Create a setup screen shown before the main UI when assets are missing.
- [x] Create `data/config.json` with setup status and installed asset versions.
- [x] Create `data/ffmpeg/`, `data/models/`, `data/cache/`, `data/jobs/`, and `data/logs/`.
- [x] Download FFmpeg during setup instead of bundling it.
- [ ] Verify FFmpeg download integrity where possible.
- [ ] Add FFmpeg source/license text to the setup screen.
- [ ] Trigger or download Demucs model weights during setup instead of bundling them.
- [x] Keep model/cache files inside the portable `data/` tree.
- [x] Show setup progress, current action, retry, and failure messages.
- [ ] Block the main app until required setup checks pass.
- [x] Block the main app until FFmpeg setup passes.
- [x] Persist real FFmpeg readiness after successful setup.
- [ ] Persist real model readiness after successful setup.

### Portable Python Runtime

- [ ] Decide between Python embeddable distribution and a portable venv-style runtime.
- [ ] Build a Windows x64 Python runtime with all project dependencies installed.
- [ ] Verify imports for FastAPI, Uvicorn, yt-dlp, Demucs, Torch, torchaudio, librosa, pyloudnorm, and soundfile.
- [ ] Confirm Torch/torchaudio versions match the project pins.
- [ ] Remove dev-only packages from the runtime.
- [ ] Confirm backend can start from the packaged runtime without system Python.

### Packaging

- [x] Define final portable folder layout.
- [ ] Configure Tauri/build scripts to produce or stage a standalone `.exe`, not an installer as the release artifact.
- [ ] Copy backend files into the release folder.
- [ ] Copy static UI files into the release folder.
- [ ] Copy Python runtime into the release folder.
- [ ] Build the Tauri Windows executable.
- [ ] Add `THIRD_PARTY_NOTICES.txt`.
- [ ] Add `licenses/` folder with bundled dependency license texts.
- [ ] Add a short `README-WINDOWS.txt` with unzip/run/troubleshooting steps.
- [ ] Zip the final folder as `StemDeck-alpha-windows-x64.zip`.
- [ ] Document expected zip size and extracted size for the release notes.
- [ ] Add Windows-friendly portable staging script.
- [ ] Verify the zip can be extracted and run from a user-writable folder such as `Downloads` or `Desktop`.

### QA

- [x] Run backend health/config/job/API regression tests locally.
- [x] Run Rust formatting check for the Tauri code.
- [x] Run git whitespace validation.
- [x] Run `cargo check` successfully with Rust `1.88+`.
- [ ] Test on a clean Windows 11 VM with no system Python.
- [ ] Test first launch with empty `data/`.
- [ ] Test setup retry after a failed FFmpeg/model download.
- [ ] Test processing the reference YouTube URL end-to-end.
- [ ] Test app close while a job is running.
- [ ] Test app relaunch after successful setup.
- [ ] Test deleting `data/ffmpeg/` and re-running setup.
- [ ] Test deleting model cache and re-running setup.
- [ ] Verify no backend console window remains open after exit.
- [ ] Verify jobs/logs are written inside the portable folder.
- [ ] Verify Help opens GitHub issues in the system browser.

### Release

- [ ] Create alpha release notes.
- [ ] Include known limitations: large download, internet required on first launch, Windows 11 target.
- [ ] Upload `StemDeck-alpha-windows-x64.zip`.
- [ ] Do not upload MSI/NSIS installer artifacts for alpha.
- [ ] Publish checksum for the zip.
- [ ] Add GitHub issue template for Windows launcher bugs.
- [ ] Collect first-user feedback before considering any installer/offline packaging later.

## What To Download On First Launch

Do not bundle these in the alpha portable zip:

- FFmpeg binaries
- Demucs model weights

Reasons:

- Smaller initial zip.
- Fewer redistribution and license concerns.
- Easier to update FFmpeg/model URLs independently of the app.
- Clearer product behavior during alpha.

## First-Launch Setup Flow

On first launch, Tauri should show a setup screen before opening the main app.

Example states:

```text
Preparing StemDeck

✓ Checking WebView runtime
✓ Checking local runtime
✓ Creating workspace
↓ Downloading FFmpeg
↓ Downloading AI separation model
✓ Verifying audio engine

Ready
```

Required behavior:

- Download missing runtime assets.
- Show progress and current file/action.
- Verify checksums where possible.
- Retry failed downloads.
- Persist setup status in `data/config.json`.
- Never silently fail into the main app if audio processing is unavailable.

## Runtime Behavior

When `StemDeck.exe` starts:

1. Resolve the portable app root folder.
2. Ensure `data/` folders exist.
3. Run first-launch setup if required.
4. Add bundled/downloaded FFmpeg to `PATH`.
5. Set model/cache environment variables.
6. Start the FastAPI backend as a child process.
7. Wait for a health endpoint.
8. Open the Tauri WebView pointed at the local backend URL.
9. Kill the backend process when Tauri exits.

The app should not write outside the extracted portable folder except for
normal OS/browser WebView cache behavior that Tauri/WebView2 may manage
internally. StemDeck-controlled jobs, logs, downloads, models, and setup state
must stay under the portable `data/` directory.

Use a random free localhost port where possible. A fixed port is acceptable for an early prototype, but random port selection avoids collisions.

## Environment Variables

Keep all generated files inside the portable folder.

Suggested environment:

```text
PATH=<app>/data/ffmpeg;%PATH%
STEMDECK_DATA_DIR=<app>/data
XDG_CACHE_HOME=<app>/data/cache
TORCH_HOME=<app>/data/models/torch
```

The backend should read `STEMDECK_DATA_DIR` and place jobs, logs, caches, and downloads under that directory.

## Tauri Launcher Responsibilities

The Tauri shell should:

- Start and stop the backend.
- Hide backend console windows.
- Surface backend startup errors.
- Detect port readiness through `/health`.
- Show setup/progress UI before the main app.
- Open external links in the system browser.
- Cleanly terminate child processes on exit.
- Check for WebView2 and show a clear setup prompt if it is missing.

## Backend Changes Needed

Add or verify:

- `GET /health`
- Configurable data directory.
- Configurable FFmpeg path or reliable `PATH` lookup.
- Model/cache path configuration.
- Setup/status API if setup is managed by the backend instead of Tauri.
- Better startup errors for missing FFmpeg/model files.

## Expected Size

Without FFmpeg and model weights bundled:

```text
Zip:       roughly 900 MB - 1.8 GB
Extracted: roughly 2 GB - 4 GB
```

The size is dominated by Torch and torchaudio.

Full offline builds with FFmpeg and Demucs models would likely add:

```text
FFmpeg:       +40 MB - 100 MB zipped
Demucs model: +100 MB - 400 MB zipped
```

## Licensing Posture

For alpha:

- Do not bundle FFmpeg.
- Do not bundle Demucs model weights.
- Download those assets during setup.
- Include `THIRD_PARTY_NOTICES.txt`.
- Add an About/Licenses screen later.

This does not remove all dependency obligations, because the portable app still bundles Python packages, Tauri runtime components, Torch, and other dependencies. It does reduce the riskiest redistribution surface for FFmpeg builds and model weights.

## Third-Party Notices Reference

Add a `THIRD_PARTY_NOTICES.txt` file at the root of the portable folder:

```text
StemDeck-Windows-x64/
  StemDeck.exe
  THIRD_PARTY_NOTICES.txt
  licenses/
    python.txt
    tauri.txt
    torch.txt
    ...
```

The notices file should list bundled runtime components and Python packages. It should also explain that FFmpeg binaries and Demucs model weights are downloaded during first-run setup rather than bundled in the alpha zip.

Minimum entries to include:

- Python
- Tauri
- FastAPI
- Uvicorn
- yt-dlp
- Demucs code
- Torch
- torchaudio
- librosa
- pyloudnorm
- soundfile
- NumPy
- SciPy
- Numba
- any other packages present in the bundled `site-packages`

Suggested starter text:

```text
THIRD-PARTY NOTICES
===================

StemDeck includes third-party open-source software. Each component is
copyrighted by its respective authors and distributed under its own license.

This file is a convenience summary. Full license texts should be included in
the licenses/ directory when StemDeck is packaged for release.

Bundled Components
------------------

Python
License: Python Software Foundation License
Website: https://www.python.org/

Tauri
License: MIT or Apache-2.0, depending on component
Website: https://tauri.app/

WebView2 Runtime
Bundling policy: not bundled in the alpha portable zip
Notes: Windows 11 is assumed to provide WebView2. If missing, StemDeck should
direct the user to Microsoft's official WebView2 runtime installer.
Website: https://developer.microsoft.com/en-us/microsoft-edge/webview2/

FastAPI
License: MIT
Website: https://fastapi.tiangolo.com/

Uvicorn
License: BSD
Website: https://www.uvicorn.org/

yt-dlp
License: Unlicense
Website: https://github.com/yt-dlp/yt-dlp

Demucs
License: MIT
Website: https://github.com/facebookresearch/demucs

PyTorch / Torch
License: BSD-style
Website: https://pytorch.org/

torchaudio
License: BSD-style
Website: https://pytorch.org/audio/

librosa
License: ISC
Website: https://librosa.org/

pyloudnorm
License: MIT
Website: https://github.com/csteinmetz1/pyloudnorm

soundfile
License: BSD
Website: https://github.com/bastibe/python-soundfile

Downloaded During First-Run Setup
---------------------------------

FFmpeg binaries are not bundled in the StemDeck alpha zip. They are downloaded
during first-run setup. FFmpeg builds may be distributed under LGPL or GPL
terms depending on how they are compiled. StemDeck should display the selected
FFmpeg build source and license before or during download.

Demucs model weights are not bundled in the StemDeck alpha zip. They are
downloaded during first-run setup. StemDeck should display the model source and
license/usage terms before or during download.

Disclaimer
----------

This notice file is not legal advice. Before public release, verify the exact
licenses of every bundled package and generated binary artifact.
```

For packaging automation, generate the final notice list from the actual bundled environment rather than maintaining it by hand. Useful commands on the packaging machine:

```text
python -m pip list --format=json
python -m pip show <package>
```

The build should fail if `THIRD_PARTY_NOTICES.txt` or required license files are missing.

## Avoid For Alpha

Avoid:

- Single-file PyInstaller executable.
- Full Windows installer.
- MSI installer.
- NSIS installer.
- Setup wizard.
- Registry integration.
- Start Menu/Desktop shortcut creation as part of installation.
- Electron shell for the initial Windows alpha.
- Docker-only distribution.
- Asking users to install Python, uv, or FFmpeg manually.
- Switching away from Demucs to lower-quality engines just to reduce size.

## Milestones

1. Add portable data directory support to backend.
2. Add `/health`.
3. Build a minimal Tauri shell that starts the backend.
4. Add FFmpeg/model setup screen.
5. Package a local Windows x64 portable folder.
6. Test on a clean Windows VM.
7. Add third-party notices.
8. Publish alpha zip release.
