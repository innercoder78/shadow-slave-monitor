"""State loading, validation, migration, and atomic persistence."""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from shadow_slave_monitor.config import MAX_CHAPTER, MIN_CHAPTER, MAX_WEBNOVEL_CYCLE, WEBNOVEL_EMERGENCY_FAILURE_LIMIT, PUBLIC_SITE_CONSECUTIVE_FAILURE_LIMIT, PUBLIC_SOURCE_PARSER_REVISION, PUBLIC_SITES, ISOLATED_PUBLIC_SOURCE_NAMES, STATE_PATH, WATCHDOG_STATE_PATH
from shadow_slave_monitor.timeutil import iso_now, parse_iso_datetime
from shadow_slave_monitor.emergency import telegram_emergency_evidence_url

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
    "public_source_failures",
    "isolated_public_source_failures",
    "public_source_failure_revision",
    "source_positions",
    "webnovel_failures",
    "webnovel_failure_cycle",
    "webnovel_success_cycle",
    "emergency_last_check_ok",
    "emergency_provenance",
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
        "Chikari": True,
        "Telegram": True,
        "Novel Buddy": True,
        "ShadowSlave.Space": True,
        "FreeWebNovel": True,
        "LightNovelUp": True,
        "Novel Phoenix": False,
        "NovelPing": False,
        "SSNovel": True,
        "NovelFull": True,
        "ReadNovelFull": True,
        "FreeWebNovel.net": True,
        "Rentry": True,
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
        "public_source_failures": {},
        "isolated_public_source_failures": {},
        "public_source_failure_revision": PUBLIC_SOURCE_PARSER_REVISION,
        "source_positions": {},
        "webnovel_failures": 0,
        "webnovel_failure_cycle": None,
        "webnovel_success_cycle": None,
        "emergency_last_check_ok": None,
        "emergency_provenance": None,
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

def validate_emergency_state(state: dict[str, Any]) -> None:
    """Validate active status separately from retained notification provenance."""
    failures = state["webnovel_failures"]
    cycle = state["webnovel_failure_cycle"]
    success_cycle = state["webnovel_success_cycle"]
    checked = state["emergency_last_check_ok"]
    if type(failures) is not int or not 0 <= failures <= WEBNOVEL_EMERGENCY_FAILURE_LIMIT:
        raise StateError("webnovel_failures must be an integer between zero and four")
    for name, value in (("webnovel_failure_cycle", cycle), ("webnovel_success_cycle", success_cycle)):
        if value is not None and (type(value) is not int or not 0 <= value <= MAX_WEBNOVEL_CYCLE):
            raise StateError(f"{name} must be a bounded cycle integer or null")
    if failures and (cycle is None or state["mode"] != "watch_webnovel"
                     or state["latest_seen"] is None or state["latest_webnovel"] is None):
        raise StateError("WebNovel failure history requires an initialized authoritative monitoring phase")
    if failures and success_cycle is not None and success_cycle >= cycle:
        raise StateError("a successful cycle cannot retain consecutive failure history from that cycle or earlier")
    if success_cycle is not None and state["latest_webnovel"] is None:
        raise StateError("successful cycle requires an authoritative chapter confirmation")
    if failures == WEBNOVEL_EMERGENCY_FAILURE_LIMIT:
        if type(checked) is not bool:
            raise StateError("active emergency requires an explicit check outcome")
    elif checked is not None:
        raise StateError("inactive emergency cannot have an active check outcome")
    provenance = state["emergency_provenance"]
    telegram = next(site for site in PUBLIC_SITES if site.name == "Telegram")
    if provenance is not None:
        if (not isinstance(provenance, dict)
                or set(provenance) != {"source", "chapter", "evidence_url"}
                or provenance["source"] != "Telegram"
                or type(provenance["chapter"]) is not int
                or not telegram_emergency_evidence_url(provenance["evidence_url"], provenance["chapter"], telegram.url)):
            raise StateError("emergency_provenance must contain canonical numbered evidence from the configured source")
        if state["latest_seen"] is None or state["latest_webnovel"] is None:
            raise StateError("emergency provenance requires initialized chapter history")
        pending = state["pending_notification"]
        known = max(state["latest_seen"], pending["latest_pending_chapter"] if pending else 0)
        if provenance["chapter"] > known:
            raise StateError("emergency provenance cannot exceed delivered or pending chapter history")
    seen, official = state["latest_seen"], state["latest_webnovel"]
    if seen is not None and official is None:
        raise StateError("notified chapter history requires a confirmed authoritative baseline")
    if seen is not None and official is not None and seen > official:
        if not provenance or seen > provenance["chapter"] or state["latest_url"] != telegram.url:
            raise StateError("latest_seen beyond authority requires emergency notification provenance")
    pending = state["pending_notification"]
    if pending and official is not None and pending["latest_pending_chapter"] > official:
        if (not provenance or pending["latest_pending_chapter"] != provenance["chapter"]
                or pending["sources"] != ["Telegram"] or pending["url"] != telegram.url):
            raise StateError("pending chapter beyond authority requires emergency notification provenance")


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
    validate_emergency_state(state)
    # Absence denotes the legacy schema; do not let initial_state's current
    # default hide that one-time migration signal.
    has_revision = "public_source_failure_revision" in data
    revision = data.get("public_source_failure_revision")
    if has_revision and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 1):
        raise StateError("public_source_failure_revision must be a positive integer")
    failures = state.get("public_source_failures")
    if not isinstance(failures, dict):
        raise StateError("public_source_failures must be an object")
    enabled_names = {site.name for site in PUBLIC_SITES if site.enabled}
    # Only older inventories may contain these formerly enabled identities.
    # Validate their counts before discarding them; unknown names stay invalid.
    obsolete_names = {"Novel Phoenix", "NovelArrow", "NovelFire", "Novel Live", "Readwn", "ReChapters"}
    migrating_inventory = revision is None or revision < 8
    clean_failures: dict[str, int] = {}
    for source, count in failures.items():
        if (not isinstance(source, str)
                or source not in enabled_names and not (migrating_inventory and source in obsolete_names)):
            raise StateError("public_source_failures contains an unknown or disabled source")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count > PUBLIC_SITE_CONSECUTIVE_FAILURE_LIMIT:
            raise StateError(
                "public_source_failures counts must be integers between 1 and "
                f"{PUBLIC_SITE_CONSECUTIVE_FAILURE_LIMIT}"
            )
        if source in enabled_names:
            clean_failures[source] = count
    # Revision 7 already uses the current parsers for remaining sources. Preserve
    # their suppression; earlier parser revisions retain the existing reset.
    if revision not in (7, PUBLIC_SOURCE_PARSER_REVISION):
        clean_failures = {}
    state["public_source_failures"] = {key: clean_failures[key] for key in sorted(clean_failures)}
    state["public_source_failure_revision"] = PUBLIC_SOURCE_PARSER_REVISION
    # Additive, optional map: no parser revision bump/reset for existing sources.
    # Legacy state normalizes to empty in memory and is not rewritten merely
    # because this field was absent. It is persisted only on operational change.
    isolated_failures = state.get("isolated_public_source_failures")
    if (not isinstance(isolated_failures, dict) or any(
            name not in ISOLATED_PUBLIC_SOURCE_NAMES or type(count) is not int
            or not 1 <= count <= PUBLIC_SITE_CONSECUTIVE_FAILURE_LIMIT
            for name, count in isolated_failures.items())):
        raise StateError("isolated_public_source_failures contains invalid source counters")
    state["isolated_public_source_failures"] = dict(sorted(isolated_failures.items()))
    positions = state.get("source_positions")
    if not isinstance(positions, dict):
        raise StateError("source_positions must be an object")
    clean_positions: dict[str, dict[str, Any]] = {}
    for source, position in positions.items():
        if source != "LightNovelUp" or not isinstance(position, dict) or set(position) != {"chapter", "url"}:
            raise StateError("source_positions contains an unknown source or invalid fields")
        chapter = valid_chapter(position.get("chapter"), allow_none=False)
        url = position.get("url")
        if not isinstance(url, str) or "%" in url:
            raise StateError("source_positions contains an invalid URL")
        try:
            parsed = urlparse(url)
        except ValueError as exc:
            raise StateError("source_positions contains an invalid URL") from exc
        host = (parsed.hostname or "").casefold()
        match = re.fullmatch(
            r"/novel/shadow-slave/chapter-(\d{1,5})-[a-z0-9]+(?:-[a-z0-9]+)*/?",
            parsed.path,
            flags=re.IGNORECASE,
        )
        if (parsed.scheme != "https" or host not in {"lightnovelup.com", "www.lightnovelup.com"}
                or parsed.netloc.casefold() != host or parsed.params or parsed.query or parsed.fragment
                or not match or int(match.group(1)) != chapter):
            raise StateError("source_positions contains a noncanonical URL")
        clean_positions[source] = {"chapter": chapter, "url": url}
    state["source_positions"] = clean_positions
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
    # A previous setup may have reached the authority but failed all public
    # checks. Resume initialization without notifying historical chapters.
    first_setup = state["latest_seen"] is None and state["pending_notification"] is None
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
    if path.exists():
        existing, _ = load_state(path)
        if all(clean[key] == existing[key] for key in clean if key != "updated_at"):
            logging.info("No operational state changes to persist.")
            return
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
