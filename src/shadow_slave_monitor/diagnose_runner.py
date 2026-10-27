"""Standalone, sequential, read-only runner comparison; never imports the monitor."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import re
import ssl

import requests
import urllib3

from shadow_slave_monitor import parsers
from shadow_slave_monitor.config import PUBLIC_SITES, STATE_PATH, WEBNOVEL_SOURCE
from shadow_slave_monitor.diagnostics import HTTP_POLICY_CODES, HtmlDocument, diagnostic_summary
from shadow_slave_monitor.emergency import telegram_emergency_evidence_url
from shadow_slave_monitor.http_client import HttpFetchError
from shadow_slave_monitor.models import ChapterReport
from shadow_slave_monitor.state_manager import StateError, validate_state


@dataclass
class SourceResult:
    category: str = "INTERNAL_ERROR"
    classification: str = "unexpected_internal_error"
    http_status: int | str = "unavailable"
    http_completed: bool = False
    chapter_validated: bool = False
    chapter: int | None = None
    baseline: int | None = None
    newer_than_baseline: bool | None = None
    detail: str | None = None


def _status(value) -> int | str:
    return value if type(value) is int and 100 <= value <= 599 else "unavailable"


@contextmanager
def _observe_transport(result: SourceResult):
    """Observe the unchanged fetch helper in this isolated, single-threaded CLI.

    Reports discard successful response metadata. Temporarily wrap the parser's
    helper to retain it, forwarding every argument and the original document
    unchanged. No additional request, HTTP policy or parser implementation.
    """
    original = parsers.fetch_html

    def observed(*args, **kwargs):
        document = original(*args, **kwargs)
        result.http_completed = True
        if isinstance(document, HtmlDocument):
            result.http_status = _status(document.metadata.status)
        return document

    parsers.fetch_html = observed
    try:
        yield
    finally:
        parsers.fetch_html = original


@contextmanager
def _quiet_source_logs():
    # The production authority checker logs titles; urllib3 debug logs can also
    # include URLs. Disable these only in this standalone diagnostic process.
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def _failure(result: SourceResult, source, exc: Exception) -> SourceResult:
    status = getattr(exc, "status", None)
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
    if _status(status) != "unavailable":
        result.http_status = _status(status)
    if isinstance(exc, requests.exceptions.SSLError):
        result.category, result.classification = "TLS_ERROR", "tls_certificate_or_handshake_error"
    elif isinstance(exc, requests.HTTPError):
        result.category = "HTTP_ERROR"
        result.classification = {
            401: "access_denied", 403: "access_denied", 429: "rate_limited",
        }.get(result.http_status, "server_error" if isinstance(result.http_status, int)
              and result.http_status >= 500 else "http_status_failure")
    elif isinstance(exc, HttpFetchError):
        result.category = "HTTP_POLICY_ERROR"
        result.classification = exc.reason if exc.reason in HTTP_POLICY_CODES else "http_policy_error"
    elif isinstance(exc, requests.Timeout):
        result.category, result.classification = "NETWORK_ERROR", "timeout"
    elif isinstance(exc, requests.ConnectionError):
        result.category, result.classification = "NETWORK_ERROR", "connection_error"
    elif isinstance(exc, requests.RequestException):
        result.category, result.classification = "NETWORK_ERROR", "request_error"
    elif isinstance(exc, parsers.ParseError):
        result.category = "PARSE_ERROR"
        result.classification = parsers.parser_code(exc.reason)
    else:
        return result
    # The shared renderer allowlists reasons, codes, counters and source hosts.
    # TLS is classified above because the shared renderer groups it with connection errors.
    if result.category != "TLS_ERROR":
        result.detail = diagnostic_summary(source, exc)
    return result


def diagnose_webnovel() -> SourceResult:
    result = SourceResult()
    with _quiet_source_logs(), _observe_transport(result):
        try:
            report = parsers.require_valid_webnovel_report(parsers.check_webnovel(WEBNOVEL_SOURCE))
            result.category, result.classification = "SUCCESS_VALID_CHAPTER", "validated_authoritative_chapter"
            result.chapter_validated, result.chapter = True, report.chapter
        except Exception as exc:
            _failure(result, WEBNOVEL_SOURCE, exc)
    return result


def _baseline_snapshot(state_path: Path) -> tuple[int, str]:
    # Read once: validation, digest and baseline must all refer to the same bytes.
    # Unlike load_state(), a missing snapshot must never initialize default state.
    raw = state_path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise StateError("snapshot_not_object")
    state = validate_state(data)
    latest = state["latest_seen"]
    if parsers.chapter_validity_category(latest) is not None:
        raise StateError("snapshot_has_no_confirmed_baseline")
    pending = state["pending_notification"]
    baseline = max(latest, pending["latest_pending_chapter"] if pending else 0)
    return baseline, hashlib.sha256(raw).hexdigest()


def diagnose_telegram(baseline: int) -> SourceResult:
    result = SourceResult(baseline=baseline)
    site = next(source for source in PUBLIC_SITES if source.name == "Telegram")
    with _quiet_source_logs(), _observe_transport(result):
        try:
            report = parsers.check_telegram_emergency(site, baseline)
            # Match the monitor's post-parser provenance check, including ?single.
            if (not isinstance(report, ChapterReport) or report.source != site.name
                    or not telegram_emergency_evidence_url(report.url, report.chapter, site.url)):
                raise parsers.ParseError("chapter_data_invalid")
            result.newer_than_baseline = report.chapter > baseline
            result.category = "SUCCESS_VALID_CHAPTER" if result.newer_than_baseline else "SUCCESS_NO_ADVANCE"
            result.classification = "validated_emergency_evidence"
            result.chapter_validated, result.chapter = True, report.chapter
        except Exception as exc:
            _failure(result, site, exc)
    return result


def _version(value: str | None) -> str:
    # Environment/version fields are also bounded; never echo arbitrary env data.
    return value if isinstance(value, str) and re.fullmatch(r"[0-9][0-9A-Za-z.+_-]{0,63}", value) else "unavailable"


def runner_information() -> dict[str, str]:
    label = os.environ.get("DIAGNOSTIC_RUNNER_LABEL")
    checkout = os.environ.get("DIAGNOSTIC_CHECKOUT_SHA", "")
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        release = {}
    system = platform.system()
    image = os.environ.get("ImageOS", "")
    return {
        "requested_runner": label if label in {"ubuntu-24.04", "ubuntu-26.04"} else "unavailable",
        "actual_os": system if system in {"Linux", "Windows", "Darwin"} else "unavailable",
        "distribution": "Ubuntu" if release.get("ID") == "ubuntu" else "unavailable",
        "os_version": _version(release.get("VERSION_ID")),
        "image_os": image if re.fullmatch(r"ubuntu[0-9]{2,4}", image) else "unavailable",
        "image_version": _version(os.environ.get("ImageVersion")),
        "python": _version(platform.python_version()),
        "requests": _version(requests.__version__),
        "urllib3": _version(urllib3.__version__),
        "openssl": ".".join(str(part) for part in ssl.OPENSSL_VERSION_INFO),
        "checkout_sha": checkout if re.fullmatch(r"[0-9a-f]{40}", checkout) else "unavailable",
        "executed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def diagnose_runner(*, state_path: Path = STATE_PATH) -> dict:
    digest = "unavailable"
    try:
        baseline, digest = _baseline_snapshot(state_path)
        setup_result = None
    except (StateError, OSError, ValueError, UnicodeError):
        baseline = None
        setup_result = SourceResult(category="SETUP_ERROR", classification="invalid_or_missing_state_snapshot")
    except Exception:
        baseline = None
        setup_result = SourceResult()
    # Never condition one source check on the other source's result or report.
    webnovel = diagnose_webnovel()
    telegram = setup_result if setup_result is not None else diagnose_telegram(baseline)
    return {
        "runner": runner_information(),
        "state_snapshot_sha256": digest,
        "webnovel": asdict(webnovel),
        "telegram_emergency": asdict(telegram),
    }


def main() -> int:
    try:
        outcome = diagnose_runner()
        sources = (outcome["webnovel"], outcome["telegram_emergency"])
        healthy = all(source["chapter_validated"] for source in sources)
        summary = ("Both source checks validated chapters." if healthy else
                   "SOURCE CHECK FAILURES: inspect each source result below.")
        payload = json.dumps(outcome, indent=2, sort_keys=True)
        print(summary + "\n" + payload)
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with Path(summary_path).open("a", encoding="utf-8") as stream:
                stream.write("## Runner compatibility diagnostics\n\n" + summary + "\n\n```json\n" + payload + "\n```\n")
        if any(source["category"] in {"SETUP_ERROR", "INTERNAL_ERROR"} for source in sources):
            return 2
        if not healthy:
            print("::warning::Source check failures; see the diagnostic summary for separate results.")
        return 0
    except Exception:
        # Never let unexpected diagnostic/setup/output exceptions print a traceback.
        print("::error::Diagnostic infrastructure failure: INTERNAL_ERROR")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
