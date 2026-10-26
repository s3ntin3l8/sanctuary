#!/usr/bin/env bash
# What the demo needs on a clean runner (called by the shared visual-check workflow, after it installed
# Python and Node). `make demo` runs from the repo's .venv and builds frontend/dist itself.
set -euo pipefail

python -m venv .venv
.venv/bin/pip install --quiet -r requirements-dev.txt
npm --prefix frontend ci --no-audit --no-fund
