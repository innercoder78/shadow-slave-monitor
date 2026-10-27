"""One bounded, opportunistic lookup; never wait for the optional worker job."""
from __future__ import annotations

import io
import logging
import os
import time
from urllib.parse import urlparse
import zipfile

import requests

from shadow_slave_monitor.config import PUBLIC_SITE_CONSECUTIVE_FAILURE_LIMIT, STATE_PATH
from shadow_slave_monitor.isolated_evidence import (
    EvidenceError, MAX_EVIDENCE_BYTES, SOURCE_FAILURES, decode_object, isolated_sources,
    read_context, validate_candidate, validate_evidence,
)
from shadow_slave_monitor.models import ChapterReport, RunResult
from shadow_slave_monitor.timeutil import utc_now

MAX_ARCHIVE_BYTES = 32 * 1024
LOOKUP_BUDGET_SECONDS = 12


def enabled() -> bool:
    return os.environ.get("SHADOW_SLAVE_ISOLATED_EVIDENCE") == "true"


def artifact_name(context) -> str:
    return f"isolated-public-evidence-{context.workflow_run_id}-{context.workflow_run_attempt}"


def _get(url: str, token: str | None, deadline: float, limit: int, *, redirect: bool = False):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise EvidenceError("coordination_timeout")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # No retries; never follow a redirect carrying the API credential. Streaming
    # and a monotonic deadline also bound slow/chunked bodies.
    with requests.get(url, headers=headers, timeout=(min(2, remaining), min(2, remaining)),
                      allow_redirects=False, stream=True) as response:
        if redirect and response.status_code == 302:
            return response.headers.get("Location", "")
        if response.status_code != 200:
            raise EvidenceError("coordination_http")
        result = bytearray()
        for chunk in response.iter_content(chunk_size=4096):
            if time.monotonic() > deadline:
                raise EvidenceError("coordination_timeout")
            result.extend(chunk)
            if len(result) > limit:
                raise EvidenceError("coordination_size")
        return bytes(result)


def evidence_from_zip(raw: bytes) -> bytes:
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise EvidenceError("archive_size")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if (len(members) != 1 or members[0].filename != "evidence.json"
                or members[0].is_dir() or not 0 < members[0].file_size <= MAX_EVIDENCE_BYTES
                or members[0].flag_bits & 1):
            raise EvidenceError("archive_members")
        # Never extract paths to disk; limit decompression independently of the
        # ZIP's claimed sizes, then let the JSON contract validate the content.
        with archive.open(members[0]) as handle:
            result = handle.read(MAX_EVIDENCE_BYTES + 1)
        if len(result) > MAX_EVIDENCE_BYTES:
            raise EvidenceError("payload_size")
        return result


def download_current_evidence(context) -> bytes | None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise EvidenceError("coordination_credentials")
    deadline = time.monotonic() + LOOKUP_BUDGET_SECONDS
    prefix = f"https://api.github.com/repos/{context.repository}/actions"
    name = artifact_name(context)
    runs = decode_object(_get(
        f"{prefix}/workflows/isolated-public-worker.yml/runs?event=workflow_run&head_sha={context.checkout_sha}&per_page=5",
        token, deadline, 128 * 1024,
    ), 128 * 1024).get("workflow_runs")
    if not isinstance(runs, list) or len(runs) > 5:
        raise EvidenceError("worker_run_listing")
    matches = []
    for run in runs:
        if (not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] <= 0
                or run.get("head_sha") != context.checkout_sha or run.get("event") != "workflow_run"
                or run.get("head_branch") != "main"
                or run.get("path") != ".github/workflows/isolated-public-worker.yml"):
            raise EvidenceError("worker_run_metadata")
        data = decode_object(_get(
            f"{prefix}/runs/{run['id']}/artifacts?name={name}&per_page=10",
            token, deadline, MAX_ARCHIVE_BYTES,
        ), MAX_ARCHIVE_BYTES)
        artifacts = data.get("artifacts")
        if not isinstance(artifacts, list) or type(data.get("total_count")) is not int:
            raise EvidenceError("artifact_listing")
        if data["total_count"] == 0 and not artifacts:
            continue
        if data["total_count"] != 1 or len(artifacts) != 1:
            raise EvidenceError("artifact_ambiguous")
        matches.append(artifacts[0])
    if not matches:
        return None
    if len(matches) != 1:
        raise EvidenceError("artifact_ambiguous")
    item = matches[0]
    if (not isinstance(item, dict) or item.get("name") != name or item.get("expired") is not False
            or type(item.get("id")) is not int or item["id"] <= 0
            or type(item.get("size_in_bytes")) is not int
            or not 0 < item["size_in_bytes"] <= MAX_ARCHIVE_BYTES):
        raise EvidenceError("artifact_metadata")
    # Construct the API URL from the verified repository/id; do not trust a
    # payload-supplied download URL. Validate the one signed redirect separately.
    location = _get(f"{prefix}/artifacts/{item['id']}/zip", token, deadline, MAX_ARCHIVE_BYTES, redirect=True)
    if not isinstance(location, str) or len(location) > 8192:
        raise EvidenceError("artifact_redirect")
    parsed = urlparse(location)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port is not None
            or parsed.fragment or not host.endswith((".blob.core.windows.net", ".actions.githubusercontent.com"))):
        raise EvidenceError("artifact_redirect")
    archive = _get(location, None, deadline, MAX_ARCHIVE_BYTES)
    return evidence_from_zip(archive)


def collect(state: dict, result: RunResult) -> list:
    if not enabled():
        return []
    try:
        context = read_context(STATE_PATH)
        if not context.permits(state):
            return []
        raw = download_current_evidence(context)
        if raw is None:
            logging.info("Optional isolated evidence not ready; continuing without waiting.")
            return []
        entries = validate_evidence(raw, context, state, utc_now())
    except Exception as exc:
        # Coordination/integrity failures never increment source HTTP/parser
        # counters, reset unrelated state, or conceal the monitor's own failure.
        reason = str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__
        logging.warning("Optional isolated evidence rejected: category=%s", reason)
        result.degrade("optional isolated evidence unavailable or rejected")
        return []
    failures = state.setdefault("isolated_public_source_failures", {})
    sites = {site.name: site for site in isolated_sources()}
    reports = []
    for entry in entries:
        name, outcome = entry["source"], entry["outcome"]
        if outcome == "candidate":
            verified = validate_candidate(sites[name], entry["chapter"], entry["title"], entry["url"])
            # Only general configured source URLs enter monitor/pending state.
            reports.append(ChapterReport(name, verified.chapter, verified.title, sites[name].url, "isolated:validated_listing"))
            failures.pop(name, None)
        elif outcome == "ahead_of_target":
            failures.pop(name, None)
            logging.info("Isolated public observation above confirmed target rejected.")
        elif outcome in SOURCE_FAILURES:
            failures[name] = min(failures.get(name, 0) + 1, PUBLIC_SITE_CONSECUTIVE_FAILURE_LIMIT)
            result.degrade("optional isolated source failed: " + name + " (" + outcome + ")")
        elif outcome == "suppressed":
            result.degrade("optional isolated source temporarily suppressed: " + name)
    return reports
