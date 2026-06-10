#!/usr/bin/env python3
"""Validate, version-check, and persist permitted repository state files."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from state_manager import StateError, validate_state, validate_watchdog_state  # noqa: E402

ALLOWED = {"state.json", "watchdog_state.json"}
METADATA_NAME = "state_artifact_metadata.json"


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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


def validate_metadata(path: Path, state_file: str) -> dict[str, Any]:
    metadata = load_json_object(path)
    required = {"artifact_type", "state_file", "base_sha256", "checkout_sha", "workflow_run_id", "workflow_run_attempt"}
    missing = required - set(metadata)
    if missing:
        raise SystemExit(f"artifact metadata missing fields: {sorted(missing)}")
    expected_type = "monitor" if state_file == "state.json" else "watchdog"
    if metadata.get("artifact_type") != expected_type or metadata.get("state_file") != state_file:
        raise SystemExit("artifact metadata does not match requested state file")
    for key in required:
        if not isinstance(metadata.get(key), str) or not metadata.get(key):
            raise SystemExit(f"artifact metadata field {key} must be a non-empty string")
    return metadata


def pending_latest(state: dict[str, Any]) -> int | None:
    pending = state.get("pending_notification")
    if isinstance(pending, dict) and isinstance(pending.get("latest_pending_chapter"), int):
        return pending["latest_pending_chapter"]
    return None


def covered_chapter(state: dict[str, Any]) -> int:
    values = [v for v in (state.get("latest_seen"), pending_latest(state), state.get("target_chapter")) if isinstance(v, int)]
    return max(values, default=0)


def monitor_artifact_superseded(current: dict[str, Any], artifact: dict[str, Any]) -> bool:
    cur_seen = current.get("latest_seen") or 0
    art_seen = artifact.get("latest_seen") or 0
    cur_web = current.get("latest_webnovel") or 0
    art_web = artifact.get("latest_webnovel") or 0
    if cur_seen < art_seen or cur_web < art_web:
        return False
    art_pending_latest = pending_latest(artifact)
    if art_pending_latest is not None and max(cur_seen, pending_latest(current) or 0) < art_pending_latest:
        return False
    art_target = artifact.get("target_chapter")
    if isinstance(art_target, int) and covered_chapter(current) < art_target:
        return False
    if artifact.get("pending_notification") is None and art_target is None:
        return True
    return covered_chapter(current) >= covered_chapter(artifact)


def artifact_superseded(state_file: str, current: dict[str, Any], artifact: dict[str, Any]) -> bool:
    if current == artifact:
        return True
    if state_file == "state.json":
        return monitor_artifact_superseded(current, artifact)
    return False


def changed_paths() -> set[str]:
    out = run(["git", "status", "--porcelain=v1"], check=True).stdout.splitlines()
    paths: set[str] = set()
    for line in out:
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path)
    return paths


def abort_rebase() -> None:
    git_dir = Path(run(["git", "rev-parse", "--git-dir"]).stdout.strip())
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        run(["git", "rebase", "--abort"], check=False)


def commit_and_push(state_file: str) -> None:
    paths = changed_paths()
    unexpected = paths - {state_file}
    if unexpected:
        raise SystemExit(f"unexpected changed or untracked paths: {sorted(unexpected)}")
    if not paths:
        print("No state changes to commit.")
        return
    run(["git", "config", "user.name", "github-actions[bot]"])
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    run(["git", "add", state_file])
    if run(["git", "diff", "--cached", "--quiet", "--", state_file], check=False).returncode == 0:
        print("Permitted state file unchanged.")
        return
    run(["git", "commit", "-m", "Update monitor state"])
    for attempt in range(1, 4):
        abort_rebase()
        pull = run(["git", "pull", "--rebase", "origin", "main"], check=False)
        if pull.returncode == 0:
            push = run(["git", "push", "origin", "HEAD:main"], check=False)
            if push.returncode == 0:
                print("State commit pushed.")
                return
            print(push.stdout)
        else:
            print(pull.stdout)
        abort_rebase()
        time.sleep(5 * attempt)
    raise SystemExit("Failed to push permitted state files after 3 attempts")


def apply_artifact(artifact_dir: Path, state_file: str) -> str:
    artifact_state_path = artifact_dir / state_file
    metadata_path = artifact_dir / METADATA_NAME
    if not artifact_state_path.exists():
        raise SystemExit(f"artifact is missing {state_file}")
    if not metadata_path.exists():
        raise SystemExit(f"artifact is missing {METADATA_NAME}")
    artifact_state = validate_state_file(artifact_state_path, state_file)
    metadata = validate_metadata(metadata_path, state_file)
    current_path = Path(state_file)
    current_state = validate_state_file(current_path, state_file)
    current_hash = sha256_file(current_path)
    base_hash = metadata["base_sha256"]
    if current_hash != base_hash:
        if sha256_file(artifact_state_path) == base_hash or artifact_superseded(state_file, current_state, artifact_state):
            print(f"Stale {state_file} artifact is semantically superseded by current repository state.")
            return "superseded"
        raise SystemExit(f"stale {state_file} artifact conflicts with current repository state; refusing overwrite")
    current_path.write_bytes(artifact_state_path.read_bytes())
    validate_state_file(current_path, state_file)
    return "applied"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("state_file", choices=sorted(ALLOWED))
    parser.add_argument("--artifact-dir", type=Path, required=True)
    args = parser.parse_args()
    outcome = apply_artifact(args.artifact_dir, args.state_file)
    if outcome == "applied":
        commit_and_push(args.state_file)


if __name__ == "__main__":
    main()
