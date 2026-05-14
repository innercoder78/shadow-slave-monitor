"""Watch for stale Shadow Slave monitor heartbeats and alert via ntfy."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

HEARTBEAT_PATH = Path("heartbeat.json")
WATCHDOG_STATE_PATH = Path("watchdog_state.json")
DEFAULT_STALE_HOURS = 5.0
DEFAULT_ALERT_THROTTLE_HOURS = 12.0
NTFY_URL_TEMPLATE = "https://ntfy.sh/{topic}"
NOTIFICATION_TITLE = "Shadow Slave monitor watchdog"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def parse_hours(value: str | None, default: float) -> float:
    """Parse a positive hour value from an environment variable."""
    if value is None:
        return default
    try:
        hours = float(value)
    except ValueError:
        logging.warning("Invalid hour value %r; using %.0f.", value, default)
        return default
    if hours <= 0:
        logging.warning("Invalid hour value %r; using %.0f.", value, default)
        return default
    return hours


def format_hours(hours: float) -> str:
    """Format hours plainly for alert text."""
    if hours.is_integer():
        return str(int(hours))
    return str(hours)


def utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(timezone.utc)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp, normalizing UTC values to timezone-aware datetimes."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_json_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load a JSON object from a file, returning an error message on failure."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"{path.name} is missing"
    except OSError as exc:
        return None, f"{path.name} could not be read: {exc}"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"{path.name} is malformed: {exc}"

    if not isinstance(data, dict):
        return None, f"{path.name} is malformed: expected a JSON object"
    return data, None


def load_watchdog_state() -> dict[str, Any]:
    """Load watchdog alert state if available."""
    data, error = load_json_file(WATCHDOG_STATE_PATH)
    if error:
        logging.info("%s; continuing with empty watchdog state.", error)
        return {}
    return data or {}


def get_alert_reason(now: datetime, stale_hours: float) -> tuple[str | None, str | None]:
    """Return an alert reason and last completed timestamp when heartbeat is unhealthy."""
    heartbeat, error = load_json_file(HEARTBEAT_PATH)
    if error:
        return error, None

    last_completed_raw = None
    if heartbeat:
        last_completed_raw = heartbeat.get("last_workflow_completed_at")
        if last_completed_raw is None:
            last_completed_raw = heartbeat.get("last_completed_at")
    last_completed = parse_timestamp(last_completed_raw)
    if last_completed is None:
        return "heartbeat.json is missing last_workflow_completed_at or it is invalid", None

    stale_after = timedelta(hours=stale_hours)
    if now - last_completed > stale_after:
        return (
            (
                "heartbeat.json has not recorded a completed monitor workflow "
                f"for over {format_hours(stale_hours)} hours"
            ),
            last_completed_raw,
        )

    logging.info("heartbeat.json is fresh; no watchdog alert needed.")
    return None, last_completed_raw


def is_throttled(state: dict[str, Any], now: datetime, throttle_hours: float) -> bool:
    """Return True if a previous alert is still inside the throttle window."""
    last_alert = parse_timestamp(state.get("last_alert_at"))
    if last_alert is None:
        return False
    return now - last_alert < timedelta(hours=throttle_hours)


def build_alert_body(reason: str, last_completed_at: str | None) -> str:
    """Build a plain watchdog alert body."""
    lines = [
        "Shadow Slave monitor may not be running.",
        "",
        f"{reason}.",
    ]
    if last_completed_at:
        lines.append(f"Last completed workflow run: {last_completed_at}")
    lines.extend(
        [
            "",
            "cron-job.org may be down, disabled, blocked, or unable to trigger GitHub Actions.",
        ]
    )
    return "\n".join(lines)


def send_watchdog_alert(body: str) -> bool:
    """Send a watchdog alert to the configured ntfy error topic."""
    topic = os.environ.get("NTFY_ERROR_TOPIC")
    if not topic:
        logging.warning("NTFY_ERROR_TOPIC is missing; watchdog alert was not sent.")
        return False

    try:
        response = requests.post(
            NTFY_URL_TEMPLATE.format(topic=topic),
            data=body.encode("utf-8"),
            headers={"Title": NOTIFICATION_TITLE},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logging.warning("Failed to send watchdog alert: %s", exc)
        return False

    logging.info("Watchdog alert sent.")
    return True


def write_watchdog_state(now: datetime, reason: str) -> None:
    """Persist the last successfully sent watchdog alert."""
    timestamp = now.isoformat()
    state = {
        "last_alert_at": timestamp,
        "last_alert_reason": reason,
        "updated_at": timestamp,
    }
    WATCHDOG_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    """Run the watchdog check."""
    now = utc_now()
    stale_hours = parse_hours(os.environ.get("WATCHDOG_STALE_HOURS"), DEFAULT_STALE_HOURS)
    throttle_hours = parse_hours(
        os.environ.get("WATCHDOG_ALERT_THROTTLE_HOURS"),
        DEFAULT_ALERT_THROTTLE_HOURS,
    )

    reason, last_completed_at = get_alert_reason(now, stale_hours)
    if reason is None:
        return

    state = load_watchdog_state()
    if is_throttled(state, now, throttle_hours):
        logging.info(
            "Watchdog alert suppressed because a previous alert was sent within %.0f hours.",
            throttle_hours,
        )
        return

    body = build_alert_body(reason, last_completed_at)
    if send_watchdog_alert(body):
        write_watchdog_state(now, reason)


if __name__ == "__main__":
    main()
