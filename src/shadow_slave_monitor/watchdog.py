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
from typing import Any, Callable
from urllib.parse import quote

import requests

from shadow_slave_monitor.config import MONITOR_WORKFLOW_FILE, MONITOR_WORKFLOW_PATH, WATCHDOG_IN_PROGRESS_GRACE_MINUTES, WATCHDOG_REPEAT_ALERT_HOURS, WATCHDOG_RESULT_PATH, WATCHDOG_STALE_HOURS, WATCHDOG_STATE_PATH
from shadow_slave_monitor.http_client import safe_exception_category
from shadow_slave_monitor.notifications import NotificationConfigError, NotificationDeliveryError, send_watchdog
from shadow_slave_monitor.state_manager import load_json_object, save_watchdog_state, validate_watchdog_state
from shadow_slave_monitor.timeutil import parse_iso_datetime, utc_now

API = "https://api.github.com"
EXPECTED_GITHUB_REPOSITORY = "innercoder78/shadow-slave-monitor"
MONITOR_WORKFLOW_NAME = "Shadow Slave chapter monitor"
WATCHDOG_LOOKBACK_MARGIN = timedelta(minutes=30)
MAX_WORKFLOW_RUN_PAGES = 10
MONITOR_ARTIFACT_NAME = "monitor-state"
NON_FAILED_MONITOR_RESULTS = {"healthy", "degraded"}
ALL_MONITOR_RESULTS = NON_FAILED_MONITOR_RESULTS | {"failed"}
ACTIVE_WORKFLOW_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}
GITHUB_REQUEST_ATTEMPTS = 3
GITHUB_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_ARTIFACT_RUN_LOOKUPS = 10
VERIFICATION_FRESH = "fresh"
VERIFICATION_UNCERTAIN = "uncertain"
VERIFICATION_STALE = "stale"

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


def _repository_monitor_runs(repo: str) -> list[dict[str, Any]]:
    data = github_get(f"/repos/{repo}/actions/runs?branch=main&per_page=100")
    runs = data.get("workflow_runs")
    if not isinstance(runs, list):
        raise RuntimeError("GitHub repository workflow_runs payload is malformed")
    return [
        run for run in runs
        if isinstance(run, dict)
        and run.get("head_branch") == "main"
        and run.get("path") == MONITOR_WORKFLOW_PATH
        and run.get("name") == MONITOR_WORKFLOW_NAME
        and is_monitor_workflow_run(run)
    ]


def _validated_repository_run(run: Any, repo: str, expected_id: str | None = None) -> dict[str, Any] | None:
    if not isinstance(run, dict) or run.get("head_branch") != "main" or not is_monitor_workflow_run(run):
        return None
    if run.get("path") != MONITOR_WORKFLOW_PATH or run.get("name") != MONITOR_WORKFLOW_NAME:
        return None
    identifier = run_id(run)
    if identifier is None or (expected_id is not None and identifier != expected_id):
        return None
    repository = run.get("repository")
    if isinstance(repository, dict) and repository.get("full_name") != repo:
        return None
    return run


def _repository_monitor_artifacts(
    repo: str,
    runs_by_id: dict[str, dict[str, Any]],
    discovery_cutoff: Any,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    data = github_get(f"/repos/{repo}/actions/artifacts?name={quote(MONITOR_ARTIFACT_NAME, safe='')}&per_page=100")
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list):
        raise RuntimeError("GitHub repository artifacts payload is malformed")
    selected: dict[str, dict[str, Any]] = {}
    omitted_lookups = 0
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("name") != MONITOR_ARTIFACT_NAME or artifact.get("expired") is not False:
            continue
        created_at = parse_iso_datetime(artifact.get("created_at"))
        if created_at is None:
            raise RuntimeError("GitHub repository artifact timestamp is malformed")
        associated = artifact.get("workflow_run")
        associated_id = associated.get("id") if isinstance(associated, dict) else None
        identifier = str(associated_id) if associated_id is not None else ""
        artifact_id = artifact.get("id")
        archive_url = artifact.get("archive_download_url")
        expected_url = f"{API}/repos/{repo}/actions/artifacts/{quote(str(artifact_id), safe='')}/zip"
        valid_location = (
            identifier.isdigit()
            and artifact_id is not None
            and str(artifact_id).isdigit()
            and archive_url == expected_url
        )
        if not valid_location:
            if created_at >= discovery_cutoff:
                raise RuntimeError("Recent GitHub repository artifact association is malformed")
            continue
        if identifier not in runs_by_id:
            if created_at < discovery_cutoff:
                continue
            omitted_lookups += 1
            if omitted_lookups > MAX_ARTIFACT_RUN_LOOKUPS:
                raise RuntimeError("Recent GitHub repository artifact lookup limit was exceeded")
            resolved = github_get(f"/repos/{repo}/actions/runs/{quote(identifier, safe='')}")
            validated = _validated_repository_run(resolved, repo, identifier)
            if validated is None:
                raise RuntimeError("Recent GitHub repository artifact run could not be validated")
            runs_by_id[identifier] = validated
        previous = selected.get(identifier)
        if previous is None or str(artifact.get("created_at") or "") > str(previous.get("created_at") or ""):
            selected[identifier] = artifact
    return selected, runs_by_id


def merge_run_evidence(primary_runs: list[dict[str, Any]], corroborated_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge duplicate runs without erasing an authoritative logical result."""
    merged = {run_id(run): dict(run) for run in primary_runs}
    for run in corroborated_runs:
        identifier = run_id(run)
        copy = dict(run)
        primary = merged.get(identifier)
        if "monitor_result" not in copy and primary is not None and "monitor_result" in primary:
            copy["monitor_result"] = primary["monitor_result"]
        merged[identifier] = copy
    return list(merged.values())


def corroborate_stale_history(primary_runs: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Independently verify evidence immediately before a stale alert."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo != EXPECTED_GITHUB_REPOSITORY:
        logging.warning("Stale-alert verification suppressed: repository context is unavailable.")
        return VERIFICATION_UNCERTAIN, primary_runs
    try:
        success_cutoff = utc_now() - timedelta(hours=WATCHDOG_STALE_HOURS)
        artifact_discovery_cutoff = success_cutoff - WATCHDOG_LOOKBACK_MARGIN
        repository_runs = _repository_monitor_runs(repo)
        runs_by_id = {str(run["id"]): run for run in repository_runs if run.get("id") is not None}
        artifacts, runs_by_id = _repository_monitor_artifacts(repo, runs_by_id, artifact_discovery_cutoff)
        verified: list[dict[str, Any]] = []
        result_unavailable = False
        metadata_unavailable = False
        for run in runs_by_id.values():
            copy = dict(run)
            timestamp = run_timestamp(copy)
            status = copy.get("status")
            artifact = artifacts.get(str(copy.get("id")))
            artifact_timestamp = parse_iso_datetime(artifact.get("created_at")) if artifact is not None else None
            if status == "completed" and timestamp is not None and timestamp >= artifact_discovery_cutoff:
                result = None
                if artifact is not None:
                    result = monitor_result_from_zip(github_get_bytes(str(artifact["archive_download_url"])))
                copy["monitor_result"] = result
                if result is None and timestamp >= success_cutoff:
                    result_unavailable = True
                    logging.info(
                        "Stale-alert verification found an unavailable recent monitor result; run_id=%s timestamp=%s.",
                        run_id(copy), run_timestamp_string(copy),
                    )
            elif timestamp is not None and timestamp >= success_cutoff and status not in ACTIVE_WORKFLOW_STATUSES:
                metadata_unavailable = True
                logging.info(
                    "Stale-alert verification found an indeterminate recent monitor status; run_id=%s timestamp=%s.",
                    run_id(copy), run_timestamp_string(copy),
                )
            elif timestamp is None and artifact_timestamp is not None and artifact_timestamp >= artifact_discovery_cutoff:
                metadata_unavailable = True
                logging.info(
                    "Stale-alert verification found a recent monitor artifact with an unavailable run timestamp; run_id=%s.",
                    run_id(copy),
                )
            verified.append(copy)
    except Exception as exc:
        status = exc.response.status_code if isinstance(exc, requests.HTTPError) and exc.response is not None else None
        logging.warning(
            "Stale-alert verification suppressed: GitHub evidence is incomplete; category=%s status=%s.",
            safe_exception_category(exc), status,
        )
        return VERIFICATION_UNCERTAIN, primary_runs

    primary_recent_ids = {
        run_id(run) for run in primary_runs
        if run_timestamp(run) is not None and run_timestamp(run) >= success_cutoff
    }
    repository_recent_ids = {
        run_id(run) for run in verified
        if run_timestamp(run) is not None and run_timestamp(run) >= success_cutoff
    }
    if primary_recent_ids - repository_recent_ids:
        logging.warning("Stale-alert verification suppressed: workflow histories disagree about recent monitor activity.")
        return VERIFICATION_UNCERTAIN, primary_runs

    merged = merge_run_evidence(primary_runs, verified)
    success = latest_success(verified)
    if success is not None and run_timestamp(success) >= success_cutoff:
        logging.info(
            "Stale-alert verification found a fresh monitor success; run_id=%s timestamp=%s result=%s.",
            run_id(success), run_timestamp_string(success), monitor_completion_result(success),
        )
        return VERIFICATION_FRESH, merged
    if result_unavailable or metadata_unavailable:
        logging.warning("Stale-alert verification suppressed: no fresh success was verified and recent evidence is incomplete.")
        return VERIFICATION_UNCERTAIN, primary_runs
    return VERIFICATION_STALE, merged

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

def history_regresses_behind_known_success(runs: list[dict[str, Any]], last_success_at: Any) -> bool:
    """Return whether fetched history contradicts a persisted successful completion."""
    known_success_time = parse_iso_datetime(last_success_at)
    if known_success_time is None:
        return False
    success = latest_success(runs)
    if success is not None:
        return run_timestamp(success) < known_success_time
    run_times = [timestamp for run in runs if (timestamp := run_timestamp(run)) is not None]
    return not run_times or max(run_times) < known_success_time

def active_recent_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    now = utc_now()
    active = [r for r in runs if r.get("status") in ACTIVE_WORKFLOW_STATUSES]
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

def evaluate(
    runs: list[dict[str, Any]],
    state: dict[str, Any],
    verify_stale: Callable[[list[dict[str, Any]]], tuple[str, list[dict[str, Any]]]] | None = None,
) -> tuple[dict[str, Any], bool, str]:
    now = utc_now()
    if history_regresses_behind_known_success(runs, state.get("last_success_at")):
        logging.warning("GitHub Actions history regressed behind the persisted known success; suppressing evaluation.")
        return state, False, "suppressed_regressive_history"
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
    if stale and verify_stale is not None:
        verification, corroborated_runs = verify_stale(runs)
        if verification == VERIFICATION_UNCERTAIN:
            return state, False, "suppressed_unverified_history"
        if verification not in {VERIFICATION_FRESH, VERIFICATION_STALE}:
            logging.warning("Stale-alert verification suppressed: verifier returned an invalid classification.")
            return state, False, "suppressed_unverified_history"
        return evaluate(corroborated_runs, state, verify_stale=None)
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
        new_state, changed, status = evaluate(runs, state, verify_stale=corroborate_stale_history)
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
