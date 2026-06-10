#!/usr/bin/env python3
"""Validate, version-check, and persist permitted repository state files."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from state_manager import StateError, atomic_write_json, validate_state, validate_watchdog_state  # noqa: E402
from timeutil import parse_iso_datetime  # noqa: E402

ALLOWED = {"state.json", "watchdog_state.json"}
METADATA_NAME = "state_artifact_metadata.json"
MONITOR_RESULT_NAME = "run_result.json"
WATCHDOG_RESULT_NAME = "watchdog_result.json"
MAX_PUSH_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 5

METADATA_FIELDS = {
    "artifact_type",
    "state_file",
    "base_sha256",
    "checkout_sha",
    "workflow_run_id",
    "workflow_run_attempt",
}
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")
POSITIVE_INT_RE = re.compile(r"^[1-9][0-9]*$")
MONITOR_RESULTS = {"healthy", "degraded", "failed"}
WATCHDOG_STATUSES = {
    "ok",
    "fresh",
    "suppressed_active_run",
    "throttled",
    "alert_failed",
    "alert_sent",
    "failed",
}
WATCHDOG_RESULT_FIELDS = {"changed", "status", "error_type", "error_message", "error_category"}
Classification = Literal["applicable", "superseded", "conflicting"]


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"artifact is missing {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is malformed JSON at line {exc.lineno} column {exc.colno}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return data


def validate_state_file(path: Path, state_file: str) -> dict[str, Any]:
    data = load_json_object(path)
    try:
        return validate_state(data) if state_file == "state.json" else validate_watchdog_state(data)
    except StateError as exc:
        raise SystemExit(f"{state_file} schema validation failed: {exc}") from exc


def validate_metadata(path: Path, state_file: str) -> dict[str, str]:
    metadata = load_json_object(path)
    unknown = set(metadata) - METADATA_FIELDS
    missing = METADATA_FIELDS - set(metadata)
    if unknown:
        raise SystemExit(f"artifact metadata contains unknown fields: {sorted(unknown)}")
    if missing:
        raise SystemExit(f"artifact metadata missing fields: {sorted(missing)}")
    for key in METADATA_FIELDS:
        if not isinstance(metadata.get(key), str):
            raise SystemExit(f"artifact metadata field {key} must be a string")
    expected_type = "monitor" if state_file == "state.json" else "watchdog"
    if metadata["artifact_type"] != expected_type:
        raise SystemExit("artifact metadata has the wrong artifact_type")
    if metadata["state_file"] != state_file:
        raise SystemExit("artifact metadata has the wrong state_file")
    if not HEX64_RE.fullmatch(metadata["base_sha256"]):
        raise SystemExit("artifact metadata base_sha256 must be exactly 64 hexadecimal characters")
    if not GIT_SHA_RE.fullmatch(metadata["checkout_sha"]):
        raise SystemExit("artifact metadata checkout_sha must be a full Git commit SHA")
    for key in ("workflow_run_id", "workflow_run_attempt"):
        if not POSITIVE_INT_RE.fullmatch(metadata[key]):
            raise SystemExit(f"artifact metadata {key} must be a positive numeric string")
    return dict(metadata)


def require_string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SystemExit(f"{name} must be a list of strings")
    return value


def validate_monitor_result(path: Path) -> dict[str, Any]:
    result = load_json_object(path)
    expected = {"result", "reasons", "degraded_reasons"}
    unknown = set(result) - expected
    missing = expected - set(result)
    if unknown:
        raise SystemExit(f"run_result.json contains unknown fields: {sorted(unknown)}")
    if missing:
        raise SystemExit(f"run_result.json is missing fields: {sorted(missing)}")
    if result.get("result") not in MONITOR_RESULTS:
        raise SystemExit("run_result.json result is not recognized")
    require_string_list(result.get("reasons"), "run_result.json reasons")
    require_string_list(result.get("degraded_reasons"), "run_result.json degraded_reasons")
    return result


def validate_watchdog_result(path: Path) -> dict[str, Any]:
    result = load_json_object(path)
    unknown = set(result) - WATCHDOG_RESULT_FIELDS
    missing = {"changed", "status"} - set(result)
    if unknown:
        raise SystemExit(f"watchdog_result.json contains unknown fields: {sorted(unknown)}")
    if missing:
        raise SystemExit(f"watchdog_result.json is missing fields: {sorted(missing)}")
    if not isinstance(result.get("changed"), bool):
        raise SystemExit("watchdog_result.json changed must be a Boolean")
    if result.get("status") not in WATCHDOG_STATUSES:
        raise SystemExit("watchdog_result.json status is not recognized")
    for key in WATCHDOG_RESULT_FIELDS - {"changed", "status"}:
        if key in result and result[key] is not None and not isinstance(result[key], str):
            raise SystemExit(f"watchdog_result.json {key} must be a string or null")
    return result


def validate_artifact_boundary(artifact_dir: Path, state_file: str) -> tuple[Path, dict[str, Any], dict[str, str]]:
    if not artifact_dir.is_dir():
        raise SystemExit(f"artifact directory does not exist: {artifact_dir}")
    state_path = artifact_dir / state_file
    result_name = MONITOR_RESULT_NAME if state_file == "state.json" else WATCHDOG_RESULT_NAME
    required = {state_file, result_name, METADATA_NAME}
    present = {child.name for child in artifact_dir.iterdir() if child.is_file()}
    missing = required - present
    if missing:
        raise SystemExit(f"artifact is missing required files: {sorted(missing)}")
    artifact_state = validate_state_file(state_path, state_file)
    metadata = validate_metadata(artifact_dir / METADATA_NAME, state_file)
    if state_file == "state.json":
        validate_monitor_result(artifact_dir / result_name)
    else:
        validate_watchdog_result(artifact_dir / result_name)
    return state_path, artifact_state, metadata


def pending_range(state: dict[str, Any]) -> tuple[int | None, int | None]:
    pending = state.get("pending_notification")
    if isinstance(pending, dict):
        first = pending.get("first_pending_chapter")
        latest = pending.get("latest_pending_chapter")
        return (first if isinstance(first, int) else None, latest if isinstance(latest, int) else None)
    return None, None


def max_known_chapter(state: dict[str, Any]) -> int:
    first, latest = pending_range(state)
    values = [state.get("latest_seen"), state.get("latest_webnovel"), state.get("target_chapter"), first, latest]
    return max((v for v in values if isinstance(v, int)), default=0)


def timestamp_value(value: Any) -> Any:
    return parse_iso_datetime(value) if isinstance(value, str) else None


def timestamp_not_older(current: Any, artifact: Any) -> bool:
    art_ts = timestamp_value(artifact)
    if art_ts is None:
        return True
    cur_ts = timestamp_value(current)
    return cur_ts is not None and cur_ts >= art_ts


def monitor_artifact_superseded(current: dict[str, Any], artifact: dict[str, Any]) -> bool:
    if current == artifact:
        return True
    cur_seen = current.get("latest_seen") or 0
    art_seen = artifact.get("latest_seen") or 0
    cur_web = current.get("latest_webnovel") or 0
    art_web = artifact.get("latest_webnovel") or 0
    if cur_seen < art_seen or cur_web < art_web:
        return False
    art_first, art_pending_latest = pending_range(artifact)
    cur_first, cur_pending_latest = pending_range(current)
    if art_pending_latest is not None:
        pending_covered = cur_seen >= art_pending_latest or (
            cur_pending_latest is not None
            and cur_pending_latest >= art_pending_latest
            and (art_first is None or cur_first is None or cur_first <= art_first)
        )
        if not pending_covered:
            return False
    art_target = artifact.get("target_chapter")
    cur_target = current.get("target_chapter")
    if isinstance(art_target, int):
        target_covered = cur_seen >= art_target or (isinstance(cur_target, int) and cur_target >= art_target)
        if not target_covered:
            return False
    if current.get("mode") != artifact.get("mode"):
        if artifact.get("mode") == "watch_webnovel" and current.get("mode") == "watch_free_sites":
            if not isinstance(cur_target, int) or cur_target < art_web:
                return False
        elif artifact.get("mode") == "watch_free_sites" and current.get("mode") == "watch_webnovel":
            if not isinstance(art_target, int) or cur_seen < art_target or current.get("pending_notification") is not None:
                return False
        else:
            return False
    if not timestamp_not_older(current.get("last_webnovel_check"), artifact.get("last_webnovel_check")):
        return False
    cur_check = timestamp_value(current.get("last_webnovel_check"))
    art_check = timestamp_value(artifact.get("last_webnovel_check"))
    cur_skip = current.get("webnovel_skip_count") or 0
    art_skip = artifact.get("webnovel_skip_count") or 0
    if cur_check == art_check and cur_skip < art_skip:
        return False
    if max_known_chapter(current) < max_known_chapter(artifact):
        return False
    return True


def numeric_id(value: Any) -> int | None:
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def watchdog_artifact_superseded(current: dict[str, Any], artifact: dict[str, Any]) -> bool:
    if current == artifact:
        return True
    if not timestamp_not_older(current.get("last_success_at"), artifact.get("last_success_at")):
        return False
    if not timestamp_not_older(current.get("last_alert_at"), artifact.get("last_alert_at")):
        return False
    if not timestamp_not_older(current.get("resolved_at"), artifact.get("resolved_at")):
        return False

    cur_outage = current.get("current_outage_id")
    art_outage = artifact.get("current_outage_id")
    if art_outage is not None and cur_outage != art_outage:
        cur_num = numeric_id(cur_outage)
        art_num = numeric_id(art_outage)
        cur_success = timestamp_value(current.get("last_success_at"))
        art_success = timestamp_value(artifact.get("last_success_at"))
        if not ((cur_num is not None and art_num is not None and cur_num >= art_num) or (cur_success and art_success and cur_success > art_success)):
            return False

    art_open = artifact.get("open_outage_id")
    cur_open = current.get("open_outage_id")
    if art_open is not None and cur_open != art_open:
        resolved = timestamp_value(current.get("resolved_at"))
        art_alert = timestamp_value(artifact.get("last_alert_at"))
        cur_success = timestamp_value(current.get("last_success_at"))
        art_success = timestamp_value(artifact.get("last_success_at"))
        resolved_after_artifact = resolved is not None and (art_alert is None or resolved >= art_alert)
        success_after_artifact = cur_success is not None and (art_success is None or cur_success > art_success)
        if not (cur_open is None and (resolved_after_artifact or success_after_artifact)):
            return False
    if artifact.get("last_alert_outage_id") is not None and current.get("last_alert_outage_id") != artifact.get("last_alert_outage_id"):
        cur_alert = timestamp_value(current.get("last_alert_at"))
        art_alert = timestamp_value(artifact.get("last_alert_at"))
        if cur_alert is None or art_alert is None or cur_alert < art_alert:
            return False
    return True


def artifact_superseded(state_file: str, current: dict[str, Any], artifact: dict[str, Any]) -> bool:
    if state_file == "state.json":
        return monitor_artifact_superseded(current, artifact)
    return watchdog_artifact_superseded(current, artifact)


def changed_entries() -> list[tuple[str, str]]:
    out = run(["git", "status", "--porcelain=v1", "-z"], check=True).stdout
    raw = out.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(raw):
        entry = raw[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        if status.startswith("R") or status.startswith("C"):
            if index < len(raw):
                path = raw[index]
                index += 1
        entries.append((status, path))
    return entries


def assert_only_permitted_state_changed(state_file: str, *, allow_clean: bool = False) -> None:
    entries = changed_entries()
    if allow_clean and not entries:
        return
    invalid = [(status, path) for status, path in entries if path != state_file or status not in {" M", "M ", "MM"}]
    if invalid:
        formatted = [f"{status} {path}" for status, path in invalid]
        raise SystemExit(f"unexpected changed, deleted, renamed, or untracked paths: {formatted}")
    if not allow_clean and not entries:
        raise SystemExit(f"expected {state_file} to be the only changed path, but the worktree is clean")


def assert_worktree_clean() -> None:
    entries = changed_entries()
    if entries:
        formatted = [f"{status} {path}" for status, path in entries]
        raise SystemExit(f"writer checkout is not clean before applying artifact: {formatted}")


def abort_rebase() -> None:
    git_dir = Path(run(["git", "rev-parse", "--git-dir"]).stdout.strip())
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        run(["git", "rebase", "--abort"], check=False)


def fetch_and_reset_main() -> None:
    abort_rebase()
    run(["git", "fetch", "origin", "main"])
    run(["git", "reset", "--hard", "origin/main"])
    abort_rebase()
    assert_worktree_clean()


def classify_artifact(state_file: str, current_path: Path, current_state: dict[str, Any], artifact_state_path: Path, artifact_state: dict[str, Any], metadata: dict[str, str]) -> Classification:
    current_hash = sha256_file(current_path)
    artifact_hash = sha256_file(artifact_state_path)
    base_hash = metadata["base_sha256"]
    if current_hash == base_hash:
        return "applicable"
    if artifact_hash == base_hash or artifact_superseded(state_file, current_state, artifact_state):
        return "superseded"
    return "conflicting"


def apply_state_artifact(state_file: str, artifact_state_path: Path, artifact_state: dict[str, Any]) -> None:
    destination = Path(state_file)
    atomic_write_json(destination, artifact_state)
    validate_state_file(destination, state_file)
    assert_only_permitted_state_changed(state_file)


def configure_git_identity() -> None:
    run(["git", "config", "user.name", "github-actions[bot]"])
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])


def commit_state_file(state_file: str) -> bool:
    assert_only_permitted_state_changed(state_file)
    run(["git", "add", "--", state_file])
    if run(["git", "diff", "--cached", "--quiet", "--", state_file], check=False).returncode == 0:
        print("Permitted state file unchanged.")
        run(["git", "reset", "--", state_file])
        return False
    run(["git", "commit", "-m", "Update monitor state"])
    return True


def persist_with_retries(artifact_dir: Path, state_file: str) -> None:
    artifact_state_path, artifact_state, metadata = validate_artifact_boundary(artifact_dir, state_file)
    configure_git_identity()
    for attempt in range(1, MAX_PUSH_ATTEMPTS + 1):
        fetch_and_reset_main()
        current_path = Path(state_file)
        current_state = validate_state_file(current_path, state_file)
        validate_artifact_boundary(artifact_dir, state_file)
        classification = classify_artifact(state_file, current_path, current_state, artifact_state_path, artifact_state, metadata)
        if classification == "superseded":
            print(f"Stale {state_file} artifact is semantically superseded by current repository state.")
            return
        if classification == "conflicting":
            raise SystemExit(f"stale {state_file} artifact conflicts with current repository state; refusing overwrite")
        apply_state_artifact(state_file, artifact_state_path, artifact_state)
        if not commit_state_file(state_file):
            return
        assert_only_permitted_state_changed(state_file, allow_clean=True)
        push = run(["git", "push", "origin", "HEAD:main"], check=False)
        if push.returncode == 0:
            print("State commit pushed.")
            return
        print(push.stdout)
        abort_rebase()
        if attempt == MAX_PUSH_ATTEMPTS:
            break
        time.sleep(BASE_BACKOFF_SECONDS * attempt)
    fetch_and_reset_main()
    raise SystemExit(f"Failed to push permitted {state_file} state after {MAX_PUSH_ATTEMPTS} validated attempts")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("state_file", choices=sorted(ALLOWED))
    parser.add_argument("--artifact-dir", type=Path, required=True)
    args = parser.parse_args()
    persist_with_retries(args.artifact_dir.resolve(), args.state_file)


if __name__ == "__main__":
    main()
