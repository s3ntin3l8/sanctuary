#!/bin/sh
# Entrypoint for the `ai` Celery worker (+ embedded beat).
#
# Resolves --concurrency from the DB (the single source of truth, set via the
# Settings UI) so a UI change survives container restarts. Falls back to 2 when
# the DB isn't readable yet (e.g. first boot before migrations). Live changes
# from the UI are applied via Celery remote control; this only sets the boot
# value.
set -eu

CONCURRENCY="$(python -m app.cli.worker_concurrency 2>/dev/null || echo 2)"
echo "worker-ai: starting with --concurrency=${CONCURRENCY}"

exec python -m celery -A app.tasks.celery_app worker -B \
    -n ai@%h --loglevel=INFO -Q ai \
    --concurrency="${CONCURRENCY}" \
    --max-tasks-per-child=50 \
    --without-gossip --without-mingle --without-heartbeat
