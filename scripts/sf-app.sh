#!/bin/sh
# ShipFactory self-build verification app: serve the REAL dashboard plugin API
# from the candidate worktree, against an ISOLATED scratch HERMES_HOME so a
# verification session can never read or write live factory state.
set -eu
HERMES_HOME="${TMPDIR:-/tmp}/sf-verify-app-$$"
export HERMES_HOME
mkdir -p "$HERMES_HOME"
exec python - "$1" <<'PY'
import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))  # candidate worktree: shipfactory/, dashboard/

from fastapi import FastAPI
import uvicorn

spec = importlib.util.spec_from_file_location(
    "sf_verification_plugin_api", Path.cwd() / "dashboard" / "plugin_api.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

app = FastAPI()

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}

@app.get("/.shipfactory/identity")
def identity() -> dict:
    # Commit-binding probe: verification confirms the running app serves the
    # exact candidate revision. Values are injected by the Factory app-session
    # parent, never inferred (environments.py request_app_start).
    return {
        "instance_id": os.environ["SHIPFACTORY_INSTANCE_ID"],
        "head_sha": os.environ["SHIPFACTORY_HEAD_SHA"],
    }

app.include_router(module.router, prefix="/api/plugins/shipfactory")
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
PY
