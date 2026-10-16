"""Exact-set Gmail replay. Capture/verify are non-destructive; replay adds imports.

Workers and beat must be quiesced by the operator before using the existing
workspace-clear command. This script deliberately does not delete workspace data.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import SessionLocal
from app.services import gmail_replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["capture", "verify-capture", "replay", "verify"]
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="Read missing raw messages/metadata from Gmail during capture",
    )
    parser.add_argument(
        "--replace-capture",
        action="store_true",
        help="Explicitly replace an existing ready replay manifest",
    )
    args = parser.parse_args()
    with SessionLocal() as db:
        if args.action == "capture":
            value = gmail_replay.capture(
                db, online=args.online, replace=args.replace_capture
            )
            print(
                json.dumps(
                    {
                        "ready": value["ready"],
                        "selected": len(value["messages"]),
                        "unresolved": len(value["unresolved_batch_ids"]),
                        "unavailable": len(value["unavailable"]),
                    }
                )
            )
            return 0 if value["ready"] else 1
        if args.action == "verify-capture":
            print(
                json.dumps(
                    {
                        "selected": len(gmail_replay.load_manifest()["messages"]),
                        "ready": True,
                    }
                )
            )
            return 0
        value = (
            gmail_replay.replay(db)
            if args.action == "replay"
            else gmail_replay.verify(db)
        )
        print(json.dumps(value))
        if value.get("failed") or value.get("missing") or value.get("import_errors"):
            return 1
        return 2 if value.get("processing") or value.get("review_required") else 0


if __name__ == "__main__":
    raise SystemExit(main())
