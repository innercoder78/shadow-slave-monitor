"""Safe HTTP helpers for untrusted source pages."""
from __future__ import annotations

import logging
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests

from shadow_slave_monitor.config import CONNECT_TIMEOUT_SECONDS, HEADERS, HTTP_BACKOFF_SECONDS, HTTP_RETRIES, MAX_HTML_BYTES, READ_TIMEOUT_SECONDS, SourceConfig

TEMPORARY_STATUSES = {429, 500, 502, 503, 504}
HTML_TYPES = {"text/html", "application/xhtml+xml", "application/xml", "text/xml"}

class HttpFetchError(RuntimeError):
    def __init__(self, reason: str, *, host: str | None = None, attempts: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.host = host
        self.attempts = attempts


def _host(url: str) -> str | None:
    return (urlparse(url).hostname or "").casefold() or None

def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = int(value.strip())
        return float(seconds) if 0 <= seconds <= 120 else None
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    delay = (dt.timestamp() - time.time())
    return max(0.0, min(delay, 120.0))

def _check_https_and_host(url: str, source: SourceConfig) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme.lower() != "https":
        raise HttpFetchError("non_https_url", host=host or None)
    if host not in {h.casefold() for h in source.allowed_hosts}:
        raise HttpFetchError("redirect_host_not_allowed", host=host or None)

def _next_url(response: requests.Response, source: SourceConfig) -> str | None:
    if not response.is_redirect:
        return None
    location = response.headers.get("Location")
    if not location:
        raise HttpFetchError("missing_redirect_location", host=_host(response.url))
    new_url = urljoin(response.url, location)
    _check_https_and_host(new_url, source)
    return new_url

def safe_exception_category(exc: BaseException) -> str:
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "connection_error"
    if isinstance(exc, requests.HTTPError):
        return "http_error"
    if isinstance(exc, requests.RequestException):
        return "request_error"
    if isinstance(exc, HttpFetchError):
        return "http_policy_error"
    return type(exc).__name__


def safe_exception_details(exc: BaseException) -> str:
    """Return controlled diagnostics without including arbitrary exception text."""
    fields: list[str] = []
    if isinstance(exc, HttpFetchError):
        fields.append(f"reason={exc.reason}")
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        fields.append(f"status={exc.response.status_code}")
    host = getattr(exc, "host", None)
    if not host and isinstance(exc, requests.RequestException) and exc.request is not None:
        host = _host(exc.request.url or "")
    if host:
        fields.append(f"host={host}")
    attempts = getattr(exc, "attempts", None)
    if attempts is not None:
        fields.append(f"attempts={attempts}")
    return " ".join(fields)

def fetch_html(source: SourceConfig, url: str | None = None) -> str:
    target = url or source.url
    _check_https_and_host(target, source)
    session = requests.Session()
    attempt = 0
    while True:
        attempt += 1
        current = target
        redirects = 0
        try:
            while True:
                response = session.get(
                    current,
                    headers=HEADERS,
                    timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                    stream=True,
                    allow_redirects=False,
                )
                nxt = _next_url(response, source)
                if nxt is not None:
                    response.close()
                    redirects += 1
                    if redirects > 5:
                        raise HttpFetchError("redirect_limit", host=_host(current), attempts=attempt)
                    current = nxt
                    continue
                break
            if response.status_code in TEMPORARY_STATUSES and attempt < HTTP_RETRIES:
                delay = _retry_after_seconds(response.headers.get("Retry-After")) or min(HTTP_BACKOFF_SECONDS * attempt, 20)
                logging.info("Temporary HTTP status %s from %s; retrying after %.1fs.", response.status_code, source.name, delay)
                response.close(); time.sleep(delay); continue
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                exc.host = _host(response.url)  # type: ignore[attr-defined]
                exc.attempts = attempt  # type: ignore[attr-defined]
                raise
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
            if content_type and content_type not in HTML_TYPES:
                raise HttpFetchError("unexpected_content_type", host=_host(response.url), attempts=attempt)
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_HTML_BYTES:
                    raise HttpFetchError("response_too_large", host=_host(response.url), attempts=attempt)
                chunks.append(chunk)
            response.encoding = response.encoding or "utf-8"
            return b"".join(chunks).decode(response.encoding, errors="replace")
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < HTTP_RETRIES:
                delay = min(HTTP_BACKOFF_SECONDS * attempt, 20)
                logging.info("Temporary %s fetching %s; retrying after %.1fs.", safe_exception_category(exc), source.name, delay)
                time.sleep(delay); continue
            exc.host = _host(current)  # type: ignore[attr-defined]
            exc.attempts = attempt  # type: ignore[attr-defined]
            raise
        except HttpFetchError as exc:
            if exc.attempts is None:
                exc.attempts = attempt
            raise
        finally:
            try:
                session.close()
            except Exception:
                pass
