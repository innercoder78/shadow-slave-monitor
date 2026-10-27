"""Standalone, sequential, read-only runner comparison; never imports the monitor."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
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
from bs4 import BeautifulSoup

from shadow_slave_monitor import http_client, parsers
from shadow_slave_monitor.config import PUBLIC_SITES, STATE_PATH, WEBNOVEL_SOURCE, SourceConfig
from shadow_slave_monitor.diagnostics import HTTP_POLICY_CODES, HtmlDocument, challenge_suspected, diagnostic_summary
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


INACTIVE_SOURCE_NAMES = ("Novel Phoenix", "NovelPing")


@dataclass
class InactiveSourceResult:
    source: str
    category: str = "INTERNAL_ERROR"
    classification: str = "unexpected_internal_error"
    http_status: int | str = "unavailable"
    http_completed: bool = False
    redirect_rejected: bool = False
    parser_available: bool = False
    parser_inspected: bool = False
    parser_category: str = "NOT_INSPECTED"
    parser_classification: str | None = None
    chapter_candidate: int | None = None
    reference_chapter: int | None = None
    newer_than_reference: bool | None = None
    jump_check: str = "not_checked_without_reference"
    evidence: dict[str, int] = field(default_factory=dict)
    additional_parser_work_required: bool | None = None
    inconclusive: bool = True
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


def _failure(
    result: SourceResult | InactiveSourceResult, source, exc: Exception,
) -> SourceResult | InactiveSourceResult:
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


def _inactive_source(name: str) -> SourceConfig:
    # Resolve exact identities from the live registry; no URLs or config clones.
    if name not in INACTIVE_SOURCE_NAMES:
        raise ValueError("invalid_inactive_source_configuration")
    matches = [source for source in PUBLIC_SITES if source.name == name]
    if len(matches) != 1 or matches[0].enabled is not False:
        raise ValueError("invalid_inactive_source_configuration")
    return matches[0]


def _inactive_evidence(soup: BeautifulSoup, site: SourceConfig) -> dict[str, int]:
    """Bounded structural observations only; never retain page text or URLs."""
    anchors = [anchor for anchor in soup.find_all("a", href=True) if parsers._inactive_visible(anchor)]
    series_path = "/novel/shadow-slave/" if site.name == "Novel Phoenix" else "/book/shadow-slave/"
    listings = {prefix + series_path + "chapters" + suffix
                for prefix in ("", *("https://" + host for host in site.allowed_hosts)) for suffix in ("", "/")}
    marker = r"^\s*Latest\s+chapter\s*:?\s*$" if site.name == "NovelPing" else r"^\s*Latest\s+Release\s*:?\s*$"
    markers = len(parsers._inactive_latest_groups(soup, marker))
    return {key: min(value, 9999) for key, value in {
        "links_inspected": len(anchors),
        "canonical_chapter_links": sum(parsers._inactive_chapter_url(anchor.get("href"), site.url, site.name)
                                       is not None for anchor in anchors),
        "series_path_links": sum(series_path in str(anchor.get("href", "")) for anchor in anchors),
        "listing_links": sum(anchor.get("href") in listings for anchor in anchors),
        "latest_release_markers": markers,
        "challenge_indicators": int(challenge_suspected(soup)),
    }.items()}


def _inactive_rejection(result: InactiveSourceResult) -> str:
    if result.evidence["challenge_indicators"]:
        return "challenge_page_suspected"
    if not result.evidence["canonical_chapter_links"] and result.evidence["listing_links"]:
        return "listing_without_individual_chapter"
    if result.source in {"Novel Phoenix", "NovelPing"} and not result.evidence["latest_release_markers"]:
        return "latest_section_missing"
    if not result.evidence["canonical_chapter_links"]:
        return "no_canonical_chapter_links"
    return "chapter_evidence_rejected"


def diagnose_inactive_source(name: str, *, reference_chapter: int | None = None) -> InactiveSourceResult:
    site = _inactive_source(name)
    if reference_chapter is not None and parsers.chapter_validity_category(reference_chapter) is not None:
        raise ValueError("invalid_reference_chapter")
    candidate_parser = {
        "Novel Phoenix": parsers.parse_novel_phoenix_candidates,
        "NovelPing": parsers.parse_novelping_candidates,
    }.get(site.name)
    result = InactiveSourceResult(
        source=site.name, reference_chapter=reference_chapter, parser_available=candidate_parser is not None,
        jump_check="not_inspected" if reference_chapter is not None else "not_checked_without_reference",
        parser_category="NOT_INSPECTED" if candidate_parser else "PARSER_UNAVAILABLE",
        additional_parser_work_required=None if candidate_parser else True,
    )
    with _quiet_source_logs():
        try:
            # One invocation of the existing bounded transport, at the configured
            # listing URL. Never call the enabled-source dispatcher or chapter pages.
            document = http_client.fetch_html(site)
            if isinstance(document, HtmlDocument):
                result.http_status = _status(document.metadata.status)
                # requests does not identify a 3xx without Location as a redirect.
                # Do not report that unresolved response as a retrieved listing.
                if isinstance(result.http_status, int) and 300 <= result.http_status < 400:
                    raise HttpFetchError("missing_redirect_location", status=result.http_status)
            result.http_completed = True
            result.category, result.classification = "HTTP_ACCESSIBLE", "retrieval_completed"
        except Exception as exc:
            _failure(result, site, exc)
            result.redirect_rejected = (
                isinstance(exc, HttpFetchError)
                and exc.reason in {"non_https_url", "redirect_host_not_allowed", "missing_redirect_location",
                                   "redirect_limit", "redirect_not_allowed"}
                and isinstance(result.http_status, int) and 300 <= result.http_status < 400
            )
            return result
        if candidate_parser is None:
            return result
        result.parser_inspected = True
        try:
            soup = BeautifulSoup(document, "html.parser")
            result.evidence = _inactive_evidence(soup, site)
            if challenge_suspected(soup):
                raise parsers.ParseError("chapter_data_missing")
            candidates = candidate_parser(soup, site.url, reference_chapter=reference_chapter)
            candidates = parsers.filter_public_candidates(candidates, site.name)
            if any(candidate.source != site.name for candidate in candidates):
                raise parsers.ParseError("chapter_data_invalid")
            # This retained validator rechecks canonical chapter URLs, including
            # rejecting a count/listing link as individual chapter evidence.
            candidates = [candidate for candidate in candidates if parsers._safe_success_url(site, candidate)]
            candidates = parsers._inactive_consensus(
                candidates, latest_only=True, reference_chapter=reference_chapter,
            )
            if candidates:
                result.chapter_candidate = max(candidate.chapter for candidate in candidates)
                if reference_chapter is not None:
                    result.newer_than_reference = result.chapter_candidate > reference_chapter
                    result.jump_check = "within_limit"
                result.parser_category = "PARSER_CANDIDATE_FOUND"
                result.additional_parser_work_required = False
                result.inconclusive = False
            else:
                result.parser_category = "PARSER_NO_TRUSTWORTHY_CANDIDATE"
                result.parser_classification = _inactive_rejection(result)
                result.additional_parser_work_required = True
        except parsers.ParseError as exc:
            result.parser_category = "PARSER_NO_TRUSTWORTHY_CANDIDATE"
            result.parser_classification = {
                "inactive_release_conflict": "contradictory_release_evidence",
                "inactive_suspicious_jump": "suspicious_chapter_jump",
                "inactive_latest_link_invalid": "latest_link_invalid_or_contradictory",
                "inactive_latest_link_missing": "latest_section_without_chapter_link",
            }.get(exc.reason, parsers.parser_code(exc.reason))
            if result.evidence.get("challenge_indicators"):
                result.parser_classification = "challenge_page_suspected"
            elif result.evidence and not result.evidence["canonical_chapter_links"] and result.evidence["listing_links"]:
                result.parser_classification = "listing_without_individual_chapter"
            if exc.reason == "inactive_suspicious_jump":
                result.jump_check = "rejected"
            result.additional_parser_work_required = True
        except Exception:
            # Retrieval succeeded; a parser crash must not become an HTTP error.
            result.parser_category = "INTERNAL_ERROR"
            result.parser_classification = "unexpected_parser_error"
            result.additional_parser_work_required = True
    return result


def diagnose_inactive_sources(*, reference_chapter: int | None = None) -> dict:
    outcome = {"diagnostic_mode": "inactive_sources", "runner": runner_information(), "sources": []}
    try:
        if reference_chapter is not None and parsers.chapter_validity_category(reference_chapter) is not None:
            raise ValueError("invalid_inactive_source_configuration")
        # Fail closed before any request if even one reference is missing/enabled.
        for name in INACTIVE_SOURCE_NAMES:
            _inactive_source(name)
    except ValueError:
        outcome["setup_error"] = "invalid_inactive_source_configuration"
        return outcome
    outcome["sources"] = [asdict(diagnose_inactive_source(name, reference_chapter=reference_chapter))
                          for name in INACTIVE_SOURCE_NAMES]
    return outcome


def main() -> int:
    try:
        mode = os.environ.get("DIAGNOSTIC_MODE", "core")
        if mode == "core":
            outcome = diagnose_runner()
            sources = (outcome["webnovel"], outcome["telegram_emergency"])
            healthy = all(source["chapter_validated"] for source in sources)
            summary = ("Both source checks validated chapters." if healthy else
                       "SOURCE CHECK FAILURES: inspect each source result below.")
        elif mode == "inactive_sources":
            raw_reference = os.environ.get("DIAGNOSTIC_REFERENCE_CHAPTER", "")
            if raw_reference and not re.fullmatch(r"[1-9][0-9]{0,4}", raw_reference):
                print("::error::Diagnostic setup failure: invalid_reference_chapter")
                return 2
            outcome = diagnose_inactive_sources(reference_chapter=int(raw_reference) if raw_reference else None)
            sources = outcome["sources"]
            healthy = (not outcome.get("setup_error") and all(
                source["category"] == "HTTP_ACCESSIBLE" and not source["inconclusive"] for source in sources
            ))
            summary = ("Inactive-source observations only; all sources remain disabled. "
                       "HTTP access and parser candidates do not establish production suitability.")
            if not healthy:
                summary += " INCONCLUSIVE OR FAILED CHECKS: inspect each source result below."
        else:
            print("::error::Diagnostic setup failure: invalid_diagnostic_mode")
            return 2
        payload = json.dumps(outcome, indent=2, sort_keys=True)
        print(summary + "\n" + payload)
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with Path(summary_path).open("a", encoding="utf-8") as stream:
                stream.write("## Runner compatibility diagnostics\n\n" + summary + "\n\n```json\n" + payload + "\n```\n")
        if (outcome.get("setup_error") or any(
            source["category"] in {"SETUP_ERROR", "INTERNAL_ERROR"}
            or source.get("parser_category") == "INTERNAL_ERROR" for source in sources
        )):
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
