"""Bounded, controlled diagnostics for untrusted public source responses."""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import requests

from shadow_slave_monitor.config import SourceConfig

HTTP_POLICY_CODES = {
    "non_https_url": "HTTP_UNSAFE_REDIRECT",
    "redirect_host_not_allowed": "HTTP_UNSAFE_REDIRECT",
    "missing_redirect_location": "HTTP_UNSAFE_REDIRECT",
    "redirect_limit": "HTTP_UNSAFE_REDIRECT",
    "unexpected_content_type": "HTTP_UNSUPPORTED_CONTENT_TYPE",
    "response_too_large": "HTTP_RESPONSE_TOO_LARGE",
}
PARSE_CODES = {
    "cursor_noncanonical": "PARSE_NONCANONICAL_CURSOR",
    "next_link_noncanonical": "PARSE_NONCANONICAL_NEXT",
    "next_link_ambiguous": "PARSE_AMBIGUOUS_NEXT",
    "next_link_non_monotonic": "PARSE_NONMONOTONIC_NEXT",
    "navigation_cycle": "PARSE_NAVIGATION_CYCLE",
    "traversal_limit": "PARSE_TRAVERSAL_LIMIT",
    "chapter_heading_conflict": "PARSE_CHAPTER_MISMATCH",
    "chapter_page_confirmation_failed": "PARSE_CHAPTER_MISMATCH",
    "WebNovel parsed chapter is outside the trusted range": "PARSE_CHAPTER_INVALID",
    "ReChapters chapter heading did not confirm listing": "PARSE_CHAPTER_MISMATCH",
    "ReChapters chapter title contradicted listing": "PARSE_CHAPTER_MISMATCH",
    "chapter heading contained contradictory numeric evidence": "PARSE_CHAPTER_MISMATCH",
    "chapter heading did not confirm expected target title": "PARSE_CHAPTER_MISMATCH",
    "NovelArrow expected title is missing": "PARSE_CHAPTER_MISMATCH",
    "NovelArrow chapter heading contradicted expected target": "PARSE_CHAPTER_MISMATCH",
    "NovelArrow document title contradicted expected target": "PARSE_CHAPTER_MISMATCH",
    "NovelArrow chapter page did not unambiguously confirm expected title": "PARSE_CHAPTER_MISMATCH",
    "Could not find WebNovel Latest Release chapter in catalog page.": "PARSE_NO_CHAPTER_LINKS",
}
REJECTION_CATEGORIES = frozenset({
    "unsafe_or_malformed", "encoded_path", "unexpected_scheme", "unexpected_authority",
    "query_fragment_or_params", "unrelated_path", "numeric_only_chapter_path",
    "title_only_chapter_path", "malformed_chapter_path", "recognized_series_nonchapter_path",
})
COUNTER_NAMES = frozenset({
    "links_inspected", "series_path_links", "canonical_chapter_links", "invalid_chapters",
    "next_links", "canonical_candidates", "ambiguous", "nonmonotonic", "challenge_indicators",
    *("rejected_" + category for category in REJECTION_CATEGORIES),
})
MAX_COUNTER = 9999


def bounded_counters(counters: dict[str, int] | None) -> dict[str, int]:
    return {
        key: max(0, min(value, MAX_COUNTER))
        for key, value in (counters or {}).items()
        if key in COUNTER_NAMES and isinstance(value, int)
    }


def safe_host(value: str | None) -> str | None:
    """Accept only bounded DNS hostnames, never a URL or arbitrary attribute."""
    if not isinstance(value, str) or len(value) > 253:
        return None
    value = value.casefold()
    return value if re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", value) else None


def host_from_url(url: str) -> str | None:
    try:
        return safe_host(urlparse(url).hostname)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ResponseMetadata:
    status: int
    host: str | None
    attempts: int


class HtmlDocument(str):
    """A string with transport metadata; plain-string parser mocks still work."""

    metadata: ResponseMetadata

    def __new__(cls, html: str, metadata: ResponseMetadata) -> HtmlDocument:
        instance = super().__new__(cls, html)
        instance.metadata = metadata
        return instance


def challenge_suspected(soup: BeautifulSoup) -> bool:
    """Require a challenge-specific DOM marker plus an interstitial heading."""
    headings = [node.get_text(" ", strip=True).casefold() for node in soup.find_all(["title", "h1"])]
    interstitial = any(text in {
        "just a moment...", "just a moment…", "checking your browser",
        "verify you are human", "attention required! | cloudflare",
    } for text in headings)
    marker = soup.find(id="challenge-form") is not None or soup.find(id="cf-challenge-running") is not None
    return bool(interstitial and marker)


def parser_code(reason: str) -> str:
    if reason.startswith("next_link_noncanonical[categories="):
        return "PARSE_NONCANONICAL_NEXT"
    if reason.startswith("Could not find any chapter links on "):
        return "PARSE_NO_CHAPTER_LINKS"
    return PARSE_CODES.get(reason, "PARSE_OTHER")


def diagnostic_summary(source: SourceConfig, exc: BaseException) -> str:
    """Render allowlisted fields only; exception text and page attributes stay private."""
    reason, stage, code = "unclassified_failure", "parse", "PARSE_OTHER"
    status = None
    if isinstance(exc, requests.Timeout):
        code, stage, reason = "NETWORK_TIMEOUT", "network", "timeout"
    elif isinstance(exc, requests.ConnectionError):
        code, stage, reason = "NETWORK_CONNECTION_ERROR", "network", "connection_error"
    elif isinstance(exc, requests.HTTPError):
        if exc.response is not None:
            status = exc.response.status_code
        code = f"HTTP_{status}" if type(status) is int and 100 <= status <= 599 else "HTTP_ERROR"
        stage, reason = "http", "http_access_denied" if status == 403 else "http_status_failure"
    elif isinstance(exc, requests.RequestException):
        code, stage, reason = "NETWORK_REQUEST_ERROR", "network", "request_error"
    elif isinstance(getattr(exc, "reason", None), str) and exc.reason in HTTP_POLICY_CODES:
        reason = exc.reason
        code, stage = HTTP_POLICY_CODES[reason], "response"
    else:
        raw_reason = getattr(exc, "reason", "")
        if isinstance(raw_reason, str):
            code = parser_code(raw_reason)
            if raw_reason in PARSE_CODES:
                reason = raw_reason
            elif code == "PARSE_NO_CHAPTER_LINKS":
                reason = "no_trustworthy_chapter_links"
            elif code == "PARSE_NONCANONICAL_NEXT":
                reason = "next_link_noncanonical"
        if code == "PARSE_NO_CHAPTER_LINKS" and getattr(exc, "code", None) == "PARSE_CHAPTER_INVALID":
            code, reason = "PARSE_CHAPTER_INVALID", "chapter_outside_trusted_range"
        if code in {"PARSE_CHAPTER_MISMATCH", "PARSE_CHAPTER_INVALID"}:
            stage = "chapter_validation"
        elif code in {"PARSE_NONCANONICAL_CURSOR", "PARSE_NONCANONICAL_NEXT", "PARSE_AMBIGUOUS_NEXT",
                      "PARSE_NONMONOTONIC_NEXT", "PARSE_NAVIGATION_CYCLE", "PARSE_TRAVERSAL_LIMIT"}:
            stage = "navigation"
        if code == "PARSE_NO_CHAPTER_LINKS" and getattr(exc, "challenge", False) is True:
            code, reason = "PAGE_CHALLENGE_SUSPECTED", "recognizable_challenge_indicators"
    if status is None:
        status = getattr(exc, "status", None)
    fields = [f"source={source.name!r}", f"code={code}", f"stage={stage}"]
    if type(status) is int and 100 <= status <= 599:
        fields.append(f"status={status}")
    # A rejected redirect's arbitrary hostname is not suitable for public logs.
    host = safe_host(getattr(exc, "host", None))
    if host in {h.casefold() for h in source.allowed_hosts}:
        fields.append(f"host={host}")
    attempts = getattr(exc, "attempts", None)
    if type(attempts) is int and 0 <= attempts <= MAX_COUNTER:
        fields.append(f"attempts={attempts}")
    fields.append(f"reason={reason!r}")
    fields.extend(f"{key}={value}" for key, value in sorted(bounded_counters(getattr(exc, "counters", None)).items()))
    return "Source check failed: " + " ".join(fields)
