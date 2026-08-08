"""State loading, validation, migration, and atomic persistence."""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from shadow_slave_monitor.config import MAX_CHAPTER, MIN_CHAPTER, PUBLIC_SITES, STATE_PATH, WATCHDOG_STATE_PATH
from shadow_slave_monitor.timeutil import iso_now, parse_iso_datetime

VALID_MODES = {"watch_webnovel", "watch_free_sites"}
LEGACY_TIMING_FIELDS = {"last_webnovel_check", "webnovel_skip_count"}

MONITOR_STATE_FIELDS = {
    "latest_seen",
    "latest_title",
    "latest_url",
    "latest_webnovel",
    "latest_webnovel_title",
    "mode",
    "target_chapter",
    "target_title",
    "target_url",
    "pending_notification",
    "updated_at",
}

WATCHDOG_STATE_FIELDS = {
    "current_outage_id",
    "open_outage_id",
    "last_alert_at",
    "last_alert_outage_id",
    "last_success_at",
    "latest_failed_conclusion",
    "latest_failed_run_url",
    "resolved_at",
}
WATCHDOG_TIMESTAMP_FIELDS = {"last_alert_at", "last_success_at", "resolved_at"}
WATCHDOG_STRING_FIELDS = WATCHDOG_STATE_FIELDS - WATCHDOG_TIMESTAMP_FIELDS

def initial_watchdog_state() -> dict[str, Any]:
    return {key: None for key in sorted(WATCHDOG_STATE_FIELDS)}

def validate_source_config() -> None:
    expected = {
        "Light Novel World": True,
        "Telegram": True,
        "Novel Buddy": True,
        "ShadowSlave.Space": True,
        "NovelArrow": False,
        "NovelFire": False,
        "NovelBin": False,
        "SSNovel": True,
        "NovelFull": True,
    }
    actual = {site.name: site.enabled for site in PUBLIC_SITES}
    if actual != expected:
        raise StateError("configured public source list or enabled statuses are invalid")

def validate_watchdog_state(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise StateError("watchdog_state.json must contain a JSON object")
    unknown = set(data) - WATCHDOG_STATE_FIELDS
    missing = WATCHDOG_STATE_FIELDS - set(data)
    if unknown:
        raise StateError(f"watchdog_state.json contains unknown fields: {sorted(unknown)}")
    if missing:
        raise StateError(f"watchdog_state.json is missing fields: {sorted(missing)}")
    clean: dict[str, Any] = {}
    for key in WATCHDOG_TIMESTAMP_FIELDS:
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or parse_iso_datetime(value) is None):
            raise StateError(f"{key} must be a valid timestamp string or null")
        clean[key] = value
    for key in WATCHDOG_STRING_FIELDS:
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise StateError(f"{key} must be a string or null")
        clean[key] = value
    return {key: clean.get(key) for key in sorted(WATCHDOG_STATE_FIELDS)}

class StateError(RuntimeError):
    pass

def parse_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None

def valid_chapter(value: Any, *, allow_none: bool = True) -> int | None:
    if value is None and allow_none:
        return None
    parsed = parse_int(value)
    if parsed is None or parsed < MIN_CHAPTER or parsed > MAX_CHAPTER:
        raise StateError(f"invalid chapter value for state field: {value!r}")
    return parsed

def initial_state() -> dict[str, Any]:
    return {
        "latest_seen": None,
        "latest_title": None,
        "latest_url": None,
        "latest_webnovel": None,
        "latest_webnovel_title": None,
        "mode": "watch_webnovel",
        "target_chapter": None,
        "target_title": None,
        "target_url": None,
        "pending_notification": None,
        "updated_at": None,
    }

def _optional_str(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateError(f"{name} must be a string or null")
    return value

def validate_pending(pending: Any, latest_seen: int | None) -> dict[str, Any] | None:
    if pending is None:
        return None
    if not isinstance(pending, dict):
        raise StateError("pending_notification must be an object or null")
    legacy_latest_value = pending.get("latest_pending_chapter", pending.get("chapter"))
    previous_seen = valid_chapter(pending.get("previous_seen"), allow_none=True)
    first_value = pending.get("first_pending_chapter")
    if first_value is None:
        first_value = (previous_seen + 1) if previous_seen is not None else legacy_latest_value
    first = valid_chapter(first_value, allow_none=False)
    latest = valid_chapter(legacy_latest_value, allow_none=False)
    if previous_seen is not None and first <= previous_seen:
        raise StateError("pending first chapter must be newer than previous_seen")
    if latest < first:
        raise StateError("pending latest chapter is lower than first chapter")
    if latest_seen is not None and latest <= latest_seen:
        raise StateError("pending latest chapter must be newer than latest_seen")
    attempts = parse_int(pending.get("attempt_count", pending.get("attempts", 0)))
    if attempts is None or attempts < 0:
        raise StateError("pending attempt_count must be a non-negative integer")
    created_at = pending.get("created_at")
    if parse_iso_datetime(created_at) is None:
        raise StateError("pending created_at must be a valid timestamp")
    retry_fields: dict[str, Any] = {}
    for key in ("first_failure_at", "last_attempt_at", "next_retry_at"):
        value = pending.get(key, created_at)
        if parse_iso_datetime(value) is None:
            raise StateError(f"pending {key} must be a valid timestamp")
        retry_fields[key] = value
    last_http_status = parse_int(pending.get("last_http_status")) if pending.get("last_http_status") is not None else None
    if pending.get("last_http_status") is not None and last_http_status is None:
        raise StateError("pending last_http_status must be an integer or null")
    migrated = {
        "previous_seen": previous_seen,
        "first_pending_chapter": first,
        "latest_pending_chapter": latest,
        "title": _optional_str(pending.get("title"), "pending.title"),
        "sources": [str(s) for s in pending.get("sources", [pending.get("source")]) if isinstance(s, str) and s.strip()],
        "url": _optional_str(pending.get("url"), "pending.url") or "",
        "created_at": created_at,
        "first_failure_at": retry_fields["first_failure_at"],
        "last_attempt_at": retry_fields["last_attempt_at"],
        "next_retry_at": retry_fields["next_retry_at"],
        "attempt_count": attempts,
        "last_error_category": _optional_str(pending.get("last_error_category"), "pending.last_error_category"),
        "last_http_status": last_http_status,
    }
    if not migrated["sources"]:
        raise StateError("pending sources must contain at least one source")
    return migrated

def validate_state(data: dict[str, Any]) -> dict[str, Any]:
    validate_source_config()
    unknown = set(data) - MONITOR_STATE_FIELDS - LEGACY_TIMING_FIELDS
    if unknown:
        raise StateError(f"state.json contains unknown fields: {sorted(unknown)}")
    state = initial_state()
    for key in state:
        if key in data:
            state[key] = copy.deepcopy(data[key])
    latest_seen = valid_chapter(state.get("latest_seen"), allow_none=True)
    latest_webnovel = valid_chapter(state.get("latest_webnovel"), allow_none=True)
    target = valid_chapter(state.get("target_chapter"), allow_none=True)
    mode = state.get("mode")
    if mode not in VALID_MODES:
        raise StateError("mode must be watch_webnovel or watch_free_sites")
    if mode == "watch_free_sites" and target is None:
        raise StateError("target_chapter is required in watch_free_sites mode")
    if latest_seen is not None and latest_webnovel is not None and latest_seen > latest_webnovel:
        raise StateError("latest_seen cannot be greater than latest_webnovel")
    if target is not None and latest_webnovel is not None and target > latest_webnovel:
        raise StateError("target_chapter cannot be greater than latest_webnovel")
    for key in ("latest_title", "latest_url", "latest_webnovel_title", "target_title", "target_url"):
        state[key] = _optional_str(state.get(key), key)
    if state.get("updated_at") is not None and parse_iso_datetime(state.get("updated_at")) is None:
        raise StateError("updated_at must be a valid timestamp or null")
    state["latest_seen"] = latest_seen
    state["latest_webnovel"] = latest_webnovel
    state["target_chapter"] = target
    state["pending_notification"] = validate_pending(state.get("pending_notification"), latest_seen)
    return state

def load_state(path: Path = STATE_PATH) -> tuple[dict[str, Any], bool]:
    if not path.exists():
        logging.info("state.json is missing; first setup run will initialize monitor state.")
        return initial_state(), True
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"state.json could not be read: {type(exc).__name__}") from exc
    if not raw.strip():
        raise StateError("state.json is empty")
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError(f"state.json is malformed JSON at line {exc.lineno} column {exc.colno}") from exc
    if not isinstance(loaded, dict):
        raise StateError("state.json must contain a JSON object")
    state = validate_state(loaded)
    first_setup = state["latest_seen"] is None and state["latest_webnovel"] is None
    return state, first_setup

def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent or Path(".")), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    clean = validate_state(state)
    clean["updated_at"] = iso_now()
    atomic_write_json(path, clean)
    logging.info("Saved state.json atomically.")

def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise StateError(f"{path.name} must be a JSON object")
    return data

def save_watchdog_state(state: dict[str, Any], path: Path = WATCHDOG_STATE_PATH) -> None:
    atomic_write_json(path, validate_watchdog_state(state))
