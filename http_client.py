"""Safe HTTP helpers for untrusted source pages."""
from __future__ import annotations

import logging
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests

from config import CONNECT_TIMEOUT_SECONDS, HEADERS, HTTP_BACKOFF_SECONDS, HTTP_RETRIES, MAX_HTML_BYTES, READ_TIMEOUT_SECONDS, SourceConfig

TEMPORARY_STATUSES = {429, 500, 502, 503, 504}
HTML_TYPES = {"text/html", "application/xhtml+xml", "application/xml", "text/xml"}

class HttpFetchError(RuntimeError):
    pass

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
    if parsed.scheme.lower() != "https":
        raise HttpFetchError(f"{source.name} URL used non-HTTPS scheme")
    host = (parsed.hostname or "").casefold()
    if host not in {h.casefold() for h in source.allowed_hosts}:
        raise HttpFetchError(f"{source.name} redirect target host is not allowed: {host or '(missing)'}")

def _next_url(response: requests.Response, source: SourceConfig) -> str | None:
    if not response.is_redirect:
        return None
    location = response.headers.get("Location")
    if not location:
        raise HttpFetchError(f"{source.name} redirect response had no Location header")
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
                        raise HttpFetchError(f"{source.name} exceeded redirect limit")
                    current = nxt
                    continue
                break
            if response.status_code in TEMPORARY_STATUSES and attempt < HTTP_RETRIES:
                delay = _retry_after_seconds(response.headers.get("Retry-After")) or min(HTTP_BACKOFF_SECONDS * attempt, 20)
                logging.info("Temporary HTTP status %s from %s; retrying after %.1fs.", response.status_code, source.name, delay)
                response.close(); time.sleep(delay); continue
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
            if content_type and content_type not in HTML_TYPES:
                raise HttpFetchError(f"{source.name} returned unexpected content type {content_type}")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_HTML_BYTES:
                    raise HttpFetchError(f"{source.name} response exceeded maximum HTML size")
                chunks.append(chunk)
            response.encoding = response.encoding or "utf-8"
            return b"".join(chunks).decode(response.encoding, errors="replace")
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < HTTP_RETRIES:
                delay = min(HTTP_BACKOFF_SECONDS * attempt, 20)
                logging.info("Temporary %s fetching %s; retrying after %.1fs.", safe_exception_category(exc), source.name, delay)
                time.sleep(delay); continue
            raise
        finally:
            try:
                session.close()
            except Exception:
                pass
