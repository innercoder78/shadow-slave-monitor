"""Strict, current-run evidence contract shared by the worker and monitor.

No source requests, notifications, or persistence occur in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from bs4 import BeautifulSoup

from shadow_slave_monitor.config import (
    ISOLATED_PUBLIC_SOURCE_NAMES, PUBLIC_SITES, TITLE_MAX_LENGTH, SourceConfig,
)
from shadow_slave_monitor.models import ChapterReport
from shadow_slave_monitor import parsers
from shadow_slave_monitor.state_manager import validate_state

SCHEMA_VERSION = 1
MAX_EVIDENCE_BYTES = 16 * 1024
MAX_AGE_SECONDS = 600
FUTURE_SKEW_SECONDS = 5
OUTCOMES = frozenset({
    "candidate", "ahead_of_target", "suppressed", "http_error", "network_error",
    "tls_error", "http_policy_error", "parse_error", "internal_error",
})
SOURCE_FAILURES = OUTCOMES - {"candidate", "ahead_of_target", "suppressed"}
BINDING_FIELDS = (
    "mode", "target_chapter", "target_title", "latest_webnovel", "latest_seen",
)
ENTRY_FIELDS = {"source", "outcome", "observed_at", "chapter", "title", "url"}
ENVELOPE_FIELDS = {
    "schema_version", "repository", "checkout_sha", "workflow_run_id",
    "workflow_run_attempt", "base_sha256", "mode", "target_chapter",
    "latest_webnovel", "produced_at", "sources",
}


class EvidenceError(ValueError):
    """Controlled reason only; never echo an untrusted payload."""


def isolated_sources() -> tuple[SourceConfig, ...]:
    # Exact configurations as well as names are an invariant. Do not allow a
    # caller to substitute a host/URL or set enabled=True to bypass isolation.
    expected = (
        SourceConfig("Novel Phoenix", "https://novelphoenix.com/novel/shadow-slave/chapters", False,
                     ("novelphoenix.com", "www.novelphoenix.com")),
        SourceConfig("NovelPing", "https://novelping.com/book/shadow-slave", False,
                     ("novelping.com", "www.novelping.com")),
    )
    selected = tuple(site for site in PUBLIC_SITES if site.name in ISOLATED_PUBLIC_SOURCE_NAMES)
    if selected != expected:
        raise EvidenceError("source_configuration")
    return selected


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("duplicate_field")
        result[key] = value
    return result


def reject_constant(_):
    raise EvidenceError("nonfinite_number")


def decode_object(raw: bytes, limit: int = MAX_EVIDENCE_BYTES) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
        raise EvidenceError("payload_size")
    try:
        result = json.loads(raw, object_pairs_hook=unique_fields, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceError("malformed_payload") from exc
    if not isinstance(result, dict):
        raise EvidenceError("payload_not_object")
    return result


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", value):
        raise EvidenceError("timestamp_format")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceError("timestamp_invalid") from exc


def utc_stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class EvidenceContext:
    repository: str
    checkout_sha: str
    workflow_run_id: str
    workflow_run_attempt: str
    base_sha256: str
    state: dict

    def provenance(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION, "repository": self.repository,
            "checkout_sha": self.checkout_sha, "workflow_run_id": self.workflow_run_id,
            "workflow_run_attempt": self.workflow_run_attempt, "base_sha256": self.base_sha256,
            "mode": self.state["mode"], "target_chapter": self.state["target_chapter"],
            "latest_webnovel": self.state["latest_webnovel"],
        }

    def permits(self, state: dict) -> bool:
        target, official, seen = (state.get(key) for key in ("target_chapter", "latest_webnovel", "latest_seen"))
        return (state.get("mode") == "watch_free_sites"
                and all(type(value) is int and parsers.chapter_validity_category(value) is None
                        for value in (target, official, seen))
                and seen < target <= official
                and all(state.get(key) == self.state.get(key) for key in BINDING_FIELDS))


def read_context(state_path: Path, *, parent_run_id: str | None = None, parent_attempt: str | None = None) -> EvidenceContext:
    raw = state_path.read_bytes()
    state = validate_state(decode_object(raw, 256 * 1024))
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "") if parent_run_id is None else parent_run_id
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "") if parent_attempt is None else parent_attempt
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not re.fullmatch(r"[0-9a-f]{40}", checkout)
            or any(not re.fullmatch(r"[1-9][0-9]{0,19}", value) for value in (run_id, attempt))):
        raise EvidenceError("missing_provenance")
    return EvidenceContext(repository, checkout, run_id, attempt, hashlib.sha256(raw).hexdigest(), state)


def validate_candidate(site: SourceConfig, chapter: Any, title: Any, url: Any) -> ChapterReport:
    if (type(chapter) is not int or parsers.chapter_validity_category(chapter) is not None
            or title is not None and (not isinstance(title, str) or not 0 < len(title) <= TITLE_MAX_LENGTH
                                      or any(ord(char) < 32 or ord(char) == 127 for char in title))
            or not isinstance(url, str) or len(url) > 2048):
        raise EvidenceError("candidate_fields")
    # Reuse the source-specific canonical URL/number/title rules on consumption,
    # independently of the producer's claim that parsing succeeded. Build a DOM
    # with text assignment so payload markup cannot become HTML evidence.
    soup = BeautifulSoup("", "html.parser")
    anchor = soup.new_tag("a", href=url)
    anchor.string = f"Chapter {chapter}" + (f": {title}" if title is not None else "")
    candidate = parsers._inactive_candidate(anchor, site.url, site.name)
    if (candidate is None or candidate.chapter != chapter or candidate.title != title
            or candidate.url != url):
        raise EvidenceError("candidate_inconsistent")
    return candidate


def validate_evidence(raw: bytes, context: EvidenceContext, state: dict, now: datetime) -> list[dict]:
    data = decode_object(raw)
    if not context.permits(state):
        raise EvidenceError("state_advanced")
    if set(data) != ENVELOPE_FIELDS or type(data.get("schema_version")) is not int:
        raise EvidenceError("envelope_fields")
    if any(type(data.get(key)) is not type(value) or data[key] != value
           for key, value in context.provenance().items()):
        raise EvidenceError("provenance_mismatch")
    produced = timestamp(data["produced_at"])
    if not -FUTURE_SKEW_SECONDS <= (now - produced).total_seconds() <= MAX_AGE_SECONDS:
        raise EvidenceError("evidence_expired")
    entries = data["sources"]
    if not isinstance(entries, list) or len(entries) != 2:
        raise EvidenceError("source_inventory")
    sites = {site.name: site for site in isolated_sources()}
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
            raise EvidenceError("entry_fields")
        name, outcome = entry["source"], entry["outcome"]
        if not isinstance(name, str) or name not in sites or name in seen:
            raise EvidenceError("source_inventory")
        seen.add(name)
        if not isinstance(outcome, str) or outcome not in OUTCOMES:
            raise EvidenceError("parser_outcome")
        observed = timestamp(entry["observed_at"])
        if (observed > produced or not -FUTURE_SKEW_SECONDS <= (now - observed).total_seconds() <= MAX_AGE_SECONDS):
            raise EvidenceError("observation_expired")
        if outcome == "candidate":
            report = validate_candidate(sites[name], entry["chapter"], entry["title"], entry["url"])
            if report.chapter > min(state["target_chapter"], state["latest_webnovel"]):
                raise EvidenceError("candidate_beyond_authority")
            if (report.chapter == state["target_chapter"] and report.title and state.get("target_title")
                    and parsers._normalized_title(report.title) != parsers._normalized_title(state["target_title"])):
                raise EvidenceError("target_title_conflict")
        elif any(entry[key] is not None for key in ("chapter", "title", "url")):
            raise EvidenceError("unexpected_candidate")
    return entries
