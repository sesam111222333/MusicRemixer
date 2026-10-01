from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import jobs as jobs_api
from app.api.router import router
from app.core.config import BSROFORMER_MODEL, DEMUCS_DEVICE, JOBS_DIR, STATIC_DIR
from app.core.persistence import load_all_jobs

# Show our INFO-level logs through uvicorn's root handler. Without this,
# Python's default root level (WARNING) silently drops every
# logger.info(...) call across the app, including the analyze
# diagnostics ("chroma:", "key candidates:").
logging.getLogger("stemdeck").setLevel(logging.INFO)
logging.getLogger("stemdeck").info("separation: %s + htdemucs_ft on %s", BSROFORMER_MODEL, DEMUCS_DEVICE)

# Pre-import librosa so the first job submission doesn't pay the 1-2 s
# cost of numpy/scipy/numba lazy initialization. Adds ~1 s to server
# boot in exchange for snappier first-job UX. Best-effort: if librosa
# isn't installed, analyze() degrades gracefully on its own.
try:
    import librosa  # noqa: F401  -- intentional warm-up import
except ImportError:
    pass

app = FastAPI(title="StemDeck")


# Force browsers to revalidate static assets on every request. Without
# this the JS/CSS modules can stick in disk cache across server
# restarts -- updated HTML loads against stale modules and the form
# silently breaks. `must-revalidate` keeps 304s working (cheap) while
# guaranteeing the latest mtime is honored.
@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# FastAPI resolves `file: UploadFile = File(...)` by having Starlette spool the
# *entire* multipart body to a temp file before the route handler ever runs --
# Starlette's MultiPartParser only caps non-file fields (max_part_size), file
# parts are written to disk unbounded. The handler's own MAX_UPLOAD_BYTES check
# therefore only fires after the whole upload is already on disk. Reject here,
# before routing/parsing starts, based on the client-declared Content-Length.
@app.middleware("http")
async def reject_oversized_uploads(request: Request, call_next):
    if request.url.path == "/api/jobs/upload":
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = None
            if declared_size is not None and declared_size > jobs_api.MAX_UPLOAD_BYTES:
                return JSONResponse(
                    {"detail": "Upload exceeds maximum allowed size"}, status_code=413
                )
    return await call_next(request)


app.include_router(router, prefix="/api")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

# Ensure the jobs directory exists at startup (module-level side effect
# moved from the old monolithic main.py; this is the canonical entrypoint).
JOBS_DIR.mkdir(exist_ok=True)
load_all_jobs()
