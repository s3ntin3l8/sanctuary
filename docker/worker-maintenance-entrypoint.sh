#!/bin/sh
# Entrypoint for the `maintenance` Celery worker.
#
# Consumes only the `maintenance` queue: the periodic recovery sweep, thread
# scan and log pruning. These must never wait behind LLM- or OCR-gated work
# (`ai` / `ingest`), because the recovery sweep exists to catch workers stuck
# on exactly that kind of long call. Fixed small concurrency (the tasks are
# short, DB-only); not resized by the Settings UI.
set -eu

echo "worker-maintenance: starting with --concurrency=2"

exec python -m celery -A app.tasks.celery_app worker \
    -n maintenance@%h --loglevel=INFO -Q maintenance \
    --concurrency=2 \
    --max-tasks-per-child=200 \
    --without-gossip --without-mingle --without-heartbeat
