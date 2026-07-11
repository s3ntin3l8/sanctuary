#!/bin/sh
# Entrypoint for the `ingest` Celery worker.
#
# Resolves --concurrency from the DB (the single source of truth, set via the
# Settings UI) so a UI change survives container restarts, and seeds the
# ocr_slots Redis limit key to the same value (app/cli/ocr_concurrency.py) so
# the per-page OCR semaphore has a live limit from the first extraction,
# even across a Redis restart that dropped the key. Falls back to 4 when the
# DB isn't readable yet (e.g. first boot before migrations) — the ocr_slots
# Lua script falls back to the same default if the limit key is absent, so
# the two stay consistent even when this script can't reach the DB. Live
# changes from the UI are applied via Celery remote control (worker pool) and
# a direct Redis SET (limit key); this only sets the boot value.
set -eu

CONCURRENCY="$(python -m app.cli.ocr_concurrency 2>/dev/null || echo 4)"
echo "worker-ingest: starting with --concurrency=${CONCURRENCY}"

exec python -m celery -A app.tasks.celery_app worker \
    -n ingest@%h --loglevel=INFO -Q ingest \
    --concurrency="${CONCURRENCY}" \
    --max-tasks-per-child=20 \
    --without-gossip --without-mingle --without-heartbeat
