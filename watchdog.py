#!/usr/bin/env python3
"""Watchdog that alerts after five hours without a successful monitor workflow."""
from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import requests

from config import MONITOR_WORKFLOW_FILE, MONITOR_WORKFLOW_PATH, WATCHDOG_IN_PROGRESS_GRACE_MINUTES, WATCHDOG_REPEAT_ALERT_HOURS, WATCHDOG_RESULT_PATH, WATCHDOG_STALE_HOURS, WATCHDOG_STATE_PATH
from http_client import safe_exception_category
from notifications import NotificationConfigError, NotificationDeliveryError, send_watchdog
from state_manager import load_json_object, save_watchdog_state, validate_watchdog_state
from timeutil import parse_iso_datetime, utc_now

API = "https://api.github.com"
MONITOR_WORKFLOW_NAME = "Shadow Slave chapter monitor"
WATCHDOG_LOOKBACK_MARGIN = timedelta(minutes=30)
MAX_WORKFLOW_RUN_PAGES = 10

def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def github_get(path: str) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.get(API + path, headers=headers, timeout=(5, 20))
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("GitHub API returned non-object JSON")
    return data

def is_monitor_workflow_run(run: dict[str, Any]) -> bool:
    path = run.get("path")
    name = run.get("name")
    event = run.get("event")
    if path is not None and path != MONITOR_WORKFLOW_PATH:
        return False
    if name is not None and name != MONITOR_WORKFLOW_NAME:
        return False
    if event in {"pull_request", "pull_request_target", "dependabot"}:
        return False
    return True

def should_continue_paging(runs: list[dict[str, Any]], found_success: bool) -> bool:
    if found_success:
        return False
    cutoff = utc_now() - timedelta(hours=WATCHDOG_STALE_HOURS) - WATCHDOG_LOOKBACK_MARGIN
    oldest: Any = None
    for run in runs:
        timestamp = parse_run_time(run, "created_at") or parse_run_time(run, "updated_at")
        if timestamp and (oldest is None or timestamp < oldest):
            oldest = timestamp
    return oldest is None or oldest > cutoff

def fetch_monitor_runs() -> list[dict[str, Any]]:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise RuntimeError("GITHUB_REPOSITORY is missing")
    collected: list[dict[str, Any]] = []
    for page in range(1, MAX_WORKFLOW_RUN_PAGES + 1):
        path = (
            f"/repos/{repo}/actions/workflows/{quote(MONITOR_WORKFLOW_FILE, safe='')}/runs"
            f"?branch=main&per_page=100&page={page}"
        )
        try:
            data = github_get(path)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            logging.error("GitHub workflow-runs API failed safely: status=%s", status)
            raise RuntimeError(f"GitHub workflow-runs API request failed with status {status}") from exc
        runs = data.get("workflow_runs")
        if not isinstance(runs, list):
            raise RuntimeError("GitHub workflow_runs payload is malformed")
        page_runs = [run for run in runs if isinstance(run, dict) and is_monitor_workflow_run(run)]
        collected.extend(page_runs)
        if len(runs) < 100 or not should_continue_paging(collected, any(r.get("status") == "completed" and r.get("conclusion") == "success" for r in collected)):
            break
    return collected

def parse_run_time(run: dict[str, Any], key: str) -> Any:
    return parse_iso_datetime(run.get(key))

def run_url(run: dict[str, Any] | None) -> str | None:
    if not run:
        return None
    url = run.get("html_url")
    return url if isinstance(url, str) else None

def run_id(run: dict[str, Any] | None) -> str | None:
    if not run:
        return None
    value = run.get("id")
    return str(value) if value is not None else None

def run_timestamp(run: dict[str, Any]) -> Any:
    return parse_run_time(run, "updated_at") or parse_run_time(run, "created_at")

def latest_success(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    successes = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") == "success" and run_timestamp(r) is not None]
    return max(successes, key=run_timestamp) if successes else None

def latest_completed(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    completed = [r for r in runs if r.get("status") == "completed" and run_timestamp(r) is not None]
    return max(completed, key=run_timestamp) if completed else None

def active_recent_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    now = utc_now()
    active = [r for r in runs if r.get("status") in {"queued", "in_progress", "waiting", "requested", "pending"}]
    recent = []
    for run in active:
        created = parse_run_time(run, "created_at")
        if created and now - created <= timedelta(minutes=WATCHDOG_IN_PROGRESS_GRACE_MINUTES):
            recent.append(run)
    return max(recent, key=lambda r: parse_run_time(r, "created_at")) if recent else None

def outage_identity(success: dict[str, Any] | None) -> str:
    if success is None:
        return "no-success-yet"
    return run_id(success) or str(success.get("updated_at"))

def build_body(success: dict[str, Any] | None, latest: dict[str, Any] | None) -> str:
    last_success_at = success.get("updated_at") if success else "never"
    latest_conclusion = latest.get("conclusion") if latest else "unknown"
    latest_url = run_url(latest) or "unavailable"
    return "\n".join([
        "Shadow Slave monitor has not completed successfully for at least five hours.",
        "",
        f"Latest successful monitor workflow: {last_success_at}",
        f"Latest completed monitor conclusion: {latest_conclusion}",
        f"Latest relevant monitor run: {latest_url}",
        "",
        "cron-job.org, GitHub Actions, external sources, notification delivery, or repository state persistence may be involved.",
    ])

def evaluate(runs: list[dict[str, Any]], state: dict[str, Any]) -> tuple[dict[str, Any], bool, str]:
    now = utc_now()
    success = latest_success(runs)
    latest = latest_completed(runs)
    active = active_recent_run(runs)
    changed = False
    ident = outage_identity(success)
    previous = state.get("current_outage_id")
    if previous and previous != ident and not state.get("resolved_at"):
        state["resolved_at"] = now.isoformat(timespec="seconds")
        changed = True
    state["current_outage_id"] = ident
    last_success_time = parse_run_time(success, "updated_at") if success else None
    stale = last_success_time is None or now - last_success_time >= timedelta(hours=WATCHDOG_STALE_HOURS)
    if active and stale:
        logging.info("Recent monitor run is active; suppressing stale alert within grace period.")
        return state, changed, "suppressed_active_run"
    if not stale:
        if state.get("open_outage_id"):
            state["open_outage_id"] = None
            state["resolved_at"] = now.isoformat(timespec="seconds")
            changed = True
        logging.info("Latest successful monitor workflow is fresh.")
        return state, changed, "fresh"
    state["open_outage_id"] = ident
    if previous != ident:
        changed = True
    last_alert_at = parse_iso_datetime(state.get("last_alert_at"))
    last_alert_outage = state.get("last_alert_outage_id")
    if last_alert_outage == ident and last_alert_at and now - last_alert_at < timedelta(hours=WATCHDOG_REPEAT_ALERT_HOURS):
        logging.info("Watchdog alert throttled for current unresolved outage.")
        return state, changed, "throttled"
    body = build_body(success, latest)
    try:
        send_watchdog(os.environ.get("NTFY_ERROR_TOPIC"), body)
    except (NotificationDeliveryError, NotificationConfigError) as exc:
        category = exc.category if isinstance(exc, NotificationDeliveryError) else "configuration_error"
        logging.warning("Watchdog ntfy delivery failed safely: category=%s status=%s", category, getattr(exc, "status_code", None))
        return state, changed, "alert_failed"
    state["last_alert_at"] = now.isoformat(timespec="seconds")
    state["last_alert_outage_id"] = ident
    state["last_success_at"] = success.get("updated_at") if success else None
    state["latest_failed_conclusion"] = latest.get("conclusion") if latest else None
    state["latest_failed_run_url"] = run_url(latest)
    state["resolved_at"] = None
    return state, True, "alert_sent"

def main() -> None:
    configure_logging()
    result = {"changed": False, "status": "ok"}
    try:
        runs = fetch_monitor_runs()
        state = validate_watchdog_state(load_json_object(WATCHDOG_STATE_PATH))
        new_state, changed, status = evaluate(runs, state)
        result = {"changed": changed, "status": status}
        if changed:
            save_watchdog_state(new_state, WATCHDOG_STATE_PATH)
    except Exception as exc:
        result = {"changed": False, "status": "failed", "error_type": type(exc).__name__}
        logging.error("Watchdog failed safely: category=%s type=%s", safe_exception_category(exc), type(exc).__name__)
        raise
    finally:
        WATCHDOG_RESULT_PATH.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__":
    main()
