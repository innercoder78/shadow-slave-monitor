#!/usr/bin/env python3
"""Watchdog that alerts after five hours without a successful monitor workflow."""
from __future__ import annotations

import io
import json
import logging
import os
import time
import zipfile
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import requests

from shadow_slave_monitor.config import MONITOR_WORKFLOW_FILE, MONITOR_WORKFLOW_PATH, WATCHDOG_IN_PROGRESS_GRACE_MINUTES, WATCHDOG_REPEAT_ALERT_HOURS, WATCHDOG_RESULT_PATH, WATCHDOG_STALE_HOURS, WATCHDOG_STATE_PATH
from shadow_slave_monitor.http_client import safe_exception_category
from shadow_slave_monitor.notifications import NotificationConfigError, NotificationDeliveryError, send_watchdog
from shadow_slave_monitor.state_manager import load_json_object, save_watchdog_state, validate_watchdog_state
from shadow_slave_monitor.timeutil import parse_iso_datetime, utc_now

API = "https://api.github.com"
MONITOR_WORKFLOW_NAME = "Shadow Slave chapter monitor"
WATCHDOG_LOOKBACK_MARGIN = timedelta(minutes=30)
MAX_WORKFLOW_RUN_PAGES = 10
MONITOR_ARTIFACT_NAME = "monitor-state"
NON_FAILED_MONITOR_RESULTS = {"healthy", "degraded"}
ALL_MONITOR_RESULTS = NON_FAILED_MONITOR_RESULTS | {"failed"}
GITHUB_REQUEST_ATTEMPTS = 3
GITHUB_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def _github_request(url: str, headers: dict[str, str], timeout: tuple[int, int]) -> requests.Response:
    for attempt in range(1, GITHUB_REQUEST_ATTEMPTS + 1):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == GITHUB_REQUEST_ATTEMPTS:
                raise
            delay = float(attempt)
            logging.info(
                "Temporary GitHub API %s; attempt=%s/%s retrying_after=%.1fs.",
                safe_exception_category(exc), attempt, GITHUB_REQUEST_ATTEMPTS, delay,
            )
            time.sleep(delay)
            continue
        if response.status_code in GITHUB_RETRYABLE_STATUSES and attempt < GITHUB_REQUEST_ATTEMPTS:
            delay = float(attempt)
            logging.info(
                "Temporary GitHub API response; status=%s attempt=%s/%s retrying_after=%.1fs.",
                response.status_code, attempt, GITHUB_REQUEST_ATTEMPTS, delay,
            )
            response.close()
            time.sleep(delay)
            continue
        response.raise_for_status()
        return response
    raise AssertionError("GitHub request retry loop exited unexpectedly")

def github_get(path: str) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = _github_request(API + path, headers, (5, 20))
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("GitHub API returned non-object JSON")
    return data

def github_get_bytes(url: str) -> bytes:
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = _github_request(url, headers, (5, 30))
    return response.content


def validate_monitor_result_payload(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    if set(data) - {"result", "reasons", "degraded_reasons"}:
        return None
    if not isinstance(data.get("reasons"), list) or any(not isinstance(item, str) for item in data["reasons"]):
        return None
    if not isinstance(data.get("degraded_reasons"), list) or any(not isinstance(item, str) for item in data["degraded_reasons"]):
        return None
    result = data.get("result")
    return result if result in ALL_MONITOR_RESULTS else None


def monitor_result_from_zip(content: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            matches = [name for name in archive.namelist() if name == "run_result.json" or name.endswith("/run_result.json")]
            if len(matches) != 1:
                return None
            with archive.open(matches[0]) as handle:
                return validate_monitor_result_payload(json.load(handle))
    except (OSError, json.JSONDecodeError, zipfile.BadZipFile):
        return None


def fetch_monitor_result_for_run(run: dict[str, Any]) -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_identifier = run.get("id")
    if not repo or run_identifier is None:
        return None
    try:
        data = github_get(f"/repos/{repo}/actions/runs/{quote(str(run_identifier), safe='')}/artifacts?per_page=100")
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        logging.warning("GitHub run-artifacts API failed safely for run %s: status=%s", run_identifier, status)
        return None
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list):
        return None
    candidates = [artifact for artifact in artifacts if isinstance(artifact, dict) and artifact.get("name") == MONITOR_ARTIFACT_NAME and not artifact.get("expired")]
    if not candidates:
        return None
    artifact = max(candidates, key=lambda item: item.get("created_at") or "")
    archive_url = artifact.get("archive_download_url")
    if not isinstance(archive_url, str):
        return None
    try:
        return monitor_result_from_zip(github_get_bytes(archive_url))
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        logging.warning("GitHub artifact download failed safely for run %s: status=%s", run_identifier, status)
        return None


def annotate_monitor_results(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = utc_now() - timedelta(hours=WATCHDOG_STALE_HOURS) - WATCHDOG_LOOKBACK_MARGIN
    annotated: list[dict[str, Any]] = []
    for run in runs:
        copy = dict(run)
        timestamp = run_timestamp(copy)
        if copy.get("status") == "completed" and timestamp is not None and timestamp >= cutoff:
            copy["monitor_result"] = fetch_monitor_result_for_run(copy)
        annotated.append(copy)
    return annotated


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

def should_continue_paging(runs: list[dict[str, Any]]) -> bool:
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
        if len(runs) < 100 or not should_continue_paging(collected):
            break
    return annotate_monitor_results(collected)

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

def run_timestamp_string(run: dict[str, Any] | None) -> str | None:
    if not run:
        return None
    for key in ("updated_at", "created_at"):
        value = run.get(key)
        if isinstance(value, str) and parse_iso_datetime(value) is not None:
            return value
    return None

def monitor_completion_result(run: dict[str, Any]) -> str | None:
    result = run.get("monitor_result")
    if result in ALL_MONITOR_RESULTS:
        return str(result)
    if "monitor_result" not in run and run.get("conclusion") == "success":
        return "healthy"
    return None


def completion_label(run: dict[str, Any] | None) -> str:
    if not run:
        return "unknown"
    return monitor_completion_result(run) or str(run.get("conclusion") or "unknown")


def latest_success(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    successes = [r for r in runs if r.get("status") == "completed" and monitor_completion_result(r) in NON_FAILED_MONITOR_RESULTS and run_timestamp(r) is not None]
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

def outage_identity(success: dict[str, Any] | None, last_success_at: str | None = None) -> str:
    if success is None:
        return last_success_at or "no-success-yet"
    return run_id(success) or str(success.get("updated_at"))

def build_body(success: dict[str, Any] | None, latest: dict[str, Any] | None, last_success_at: str | None = None) -> str:
    rendered_last_success_at = run_timestamp_string(success) or last_success_at or "never"
    latest_conclusion = completion_label(latest)
    latest_url = run_url(latest) or "unavailable"
    return "\n".join([
        "Shadow Slave monitor has not completed successfully for at least five hours.",
        "",
        f"Latest successful monitor workflow: {rendered_last_success_at}",
        f"Latest completed monitor result: {latest_conclusion}",
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
    last_success_at = run_timestamp_string(success) or state.get("last_success_at")
    last_success_time = run_timestamp(success) if success else parse_iso_datetime(last_success_at)
    ident = outage_identity(success, last_success_at)
    previous = state.get("current_outage_id")
    stale = last_success_time is None or now - last_success_time >= timedelta(hours=WATCHDOG_STALE_HOURS)
    if active and stale:
        logging.info("Recent monitor run is active; suppressing stale alert within grace period.")
        return state, changed, "suppressed_active_run"
    if not stale:
        if state.get("open_outage_id") or state.get("latest_failed_conclusion") is not None or state.get("latest_failed_run_url") is not None:
            recovery_updates = {
                "current_outage_id": ident,
                "last_success_at": last_success_at,
                "latest_failed_conclusion": None,
                "latest_failed_run_url": None,
            }
            for key, value in recovery_updates.items():
                if state.get(key) != value:
                    state[key] = value
                    changed = True
        if state.get("open_outage_id"):
            state["open_outage_id"] = None
            state["resolved_at"] = now.isoformat(timespec="seconds")
            changed = True
        logging.info("Latest successful monitor workflow is fresh.")
        return state, changed, "fresh"
    state["open_outage_id"] = ident
    state["current_outage_id"] = ident
    if previous != ident:
        changed = True
    last_alert_at = parse_iso_datetime(state.get("last_alert_at"))
    last_alert_outage = state.get("last_alert_outage_id")
    if last_alert_outage == ident and last_alert_at and now - last_alert_at < timedelta(hours=WATCHDOG_REPEAT_ALERT_HOURS):
        logging.info("Watchdog alert throttled for current unresolved outage.")
        return state, changed, "throttled"
    body = build_body(success, latest, last_success_at)
    try:
        send_watchdog(os.environ.get("NTFY_ERROR_TOPIC"), body)
    except (NotificationDeliveryError, NotificationConfigError) as exc:
        category = exc.category if isinstance(exc, NotificationDeliveryError) else "configuration_error"
        logging.warning("Watchdog ntfy delivery failed safely: category=%s status=%s", category, getattr(exc, "status_code", None))
        return state, changed, "alert_failed"
    state["last_alert_at"] = now.isoformat(timespec="seconds")
    state["last_alert_outage_id"] = ident
    state["last_success_at"] = last_success_at
    state["latest_failed_conclusion"] = completion_label(latest) if latest else None
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
        WATCHDOG_RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        WATCHDOG_RESULT_PATH.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__":
    main()
