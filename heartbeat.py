"""Write the Shadow Slave monitor heartbeat file."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_PATH = Path("heartbeat.json")


def main() -> None:
    """Write a successful monitor heartbeat."""
    timestamp = datetime.now(timezone.utc).isoformat()
    heartbeat = {
        "last_workflow_completed_at": timestamp,
        "last_status": "success",
        "workflow": "monitor",
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "github_sha": os.environ.get("GITHUB_SHA"),
        "updated_at": timestamp,
    }
    HEARTBEAT_PATH.write_text(
        json.dumps(heartbeat, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
