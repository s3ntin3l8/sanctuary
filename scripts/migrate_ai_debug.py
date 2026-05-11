#!/usr/bin/env python3
import json
import logging
import shutil
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def migrate_ai_debug_logs():
    data_dir = Path("data/ai_debug")
    if not data_dir.exists():
        logger.info("No ai_debug directory found.")
        return

    runs_file = data_dir / "runs.jsonl"
    mapping = {}

    if runs_file.exists():
        with open(runs_file, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    kind = entry.get("kind")
                    scope_id = entry.get("scope_id")
                    batch_id = entry.get("batch_id")
                    if kind and scope_id and batch_id is not None:
                        label = f"{kind}_{scope_id}"
                        mapping[label] = int(batch_id)
                except Exception:
                    continue

    moved_count = 0
    # Search recursively because some files might have already been moved to unbatched
    for log_file in list(data_dir.rglob("*.log")) + list(data_dir.rglob("*.md")):
        if not log_file.is_file():
            continue

        filename = log_file.name

        # Try to infer debug_label
        debug_label = filename[:-4] if filename.endswith(".log") else filename[:-3]

        # Determine target folder
        if filename.startswith("case_"):
            target_dir = data_dir
        else:
            base_label = debug_label
            if "-p1" in debug_label:
                base_label = debug_label.split("-p1")[0]

            batch_id = mapping.get(base_label)

            if batch_id is not None:
                target_dir = data_dir / f"ib-{batch_id:04d}"
            else:
                target_dir = data_dir / "unbatched"

        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / filename

        if target_file != log_file:
            shutil.move(str(log_file), str(target_file))
            moved_count += 1

    logger.info(f"Migration complete. Moved {moved_count} log files.")


if __name__ == "__main__":
    migrate_ai_debug_logs()
