"""ntfy notification sending and pending retry policy."""
from __future__ import annotations

import logging
from datetime import timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from shadow_slave_monitor.config import NTFY_BASE_URL, NTFY_BODY_MAX_BYTES, NTFY_CONNECT_TIMEOUT_SECONDS, NTFY_READ_TIMEOUT_SECONDS, PENDING_RETRY_DELAYS_SECONDS, PENDING_RETRY_MAX_DELAY_SECONDS, PUBLIC_SITES
from shadow_slave_monitor.http_client import safe_exception_category
from shadow_slave_monitor.models import ChapterReport
from shadow_slave_monitor.state_manager import parse_int
from shadow_slave_monitor.timeutil import iso_now, parse_iso_datetime, utc_now

class NotificationConfigError(RuntimeError):
    pass

class NotificationDeliveryError(RuntimeError):
    def __init__(self, category: str, status_code: int | None = None, retry_after_seconds: int | None = None) -> None:
        super().__init__(category)
        self.category = category
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds

def retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        seconds = int(value.strip())
        return seconds if 0 <= seconds <= PENDING_RETRY_MAX_DELAY_SECONDS else None
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    delay = int((dt.timestamp() - utc_now().timestamp()))
    return max(0, min(delay, PENDING_RETRY_MAX_DELAY_SECONDS))

NEW_CHAPTER_NOTIFICATION_TITLE = "Shadow Slave Chapter Monitor"


def new_chapter_count(previous_seen: int | None, latest_chapter: int) -> int:
    """Return the actual chapter gap, with one as the safe fallback."""
    if previous_seen is not None and previous_seen < latest_chapter:
        return latest_chapter - previous_seen
    return 1


def source_names(source: str) -> list[str]:
    """Normalize aggregated sources into configured order without losing unknowns."""
    supplied = [name.strip() for name in source.split(",") if name.strip()]
    unique = list(dict.fromkeys(supplied))
    configured = {site.name: index for index, site in enumerate(PUBLIC_SITES)}
    known = sorted((name for name in unique if name in configured), key=configured.__getitem__)
    return [*known, *(name for name in unique if name not in configured)]


def source_lines(source: str, fallback_url: str = "", *, include_urls: bool = True) -> list[str]:
    names = source_names(source)
    configured_urls = {site.name: site.url for site in PUBLIC_SITES}
    lines = []
    for name in names:
        url = configured_urls.get(name)
        if url is None and len(names) == 1:
            url = fallback_url or None
        lines.append(f"{name} [{url}]" if include_urls and url else name)
    return lines


def _fits_ntfy_body(body: str) -> bool:
    return len(body.encode("utf-8")) <= NTFY_BODY_MAX_BYTES

def notification_body(previous_seen: int | None, latest: ChapterReport) -> str:
    count = new_chapter_count(previous_seen, latest.chapter)
    availability = (
        "There is 1 new chapter available on the free sites."
        if count == 1 else f"There are {count} new chapters available on the free sites."
    )
    latest_line = f"Latest Chapter: {latest.chapter}" + (f" — {latest.title}" if latest.title else "")
    names = source_names(latest.source)
    heading = "SOURCE:" if len(names) == 1 else "SOURCES:"

    def build(lines: list[str], chapter_line: str = latest_line) -> str:
        return "\n".join([availability, chapter_line, "", heading, *lines])

    body = build(source_lines(latest.source, latest.url))
    if _fits_ntfy_body(body):
        return body

    # URLs are the least important and largest optional part of an overlong body.
    body = build(source_lines(latest.source, latest.url, include_urls=False))
    if _fits_ntfy_body(body):
        return body

    # Keep the chapter number and as many complete source names as possible.  Never
    # truncate a URL or split a Unicode character while applying this rare fallback.
    kept: list[str] = []
    for name in names:
        candidate = build([*kept, name], f"Latest Chapter: {latest.chapter}")
        if not _fits_ntfy_body(candidate):
            break
        kept.append(name)
    return build(kept, f"Latest Chapter: {latest.chapter}")

def _post_ntfy(topic: str, title: str, body: str) -> None:
    url = f"{NTFY_BASE_URL}/{topic}"
    try:
        response = requests.post(url, data=body.encode("utf-8"), headers={"Title": title}, timeout=(NTFY_CONNECT_TIMEOUT_SECONDS, NTFY_READ_TIMEOUT_SECONDS))
        retry_after = retry_after_seconds(response.headers.get("Retry-After"))
        if response.status_code >= 400:
            raise NotificationDeliveryError("http_error", response.status_code, retry_after)
    except NotificationDeliveryError:
        raise
    except requests.RequestException as exc:
        raise NotificationDeliveryError(safe_exception_category(exc), None, None) from exc

def send_new_chapter(topic: str | None, previous_seen: int | None, latest: ChapterReport) -> None:
    if not topic:
        raise NotificationConfigError("NTFY_NEWCHAPTER is missing")
    _post_ntfy(topic, NEW_CHAPTER_NOTIFICATION_TITLE, notification_body(previous_seen, latest))
    logging.info("Sent ntfy notification for chapter %s.", latest.chapter)

def send_watchdog(topic: str | None, body: str) -> None:
    if not topic:
        raise NotificationConfigError("NTFY_ERROR_TOPIC is missing")
    _post_ntfy(topic, "Shadow Slave monitor error", body)
    logging.info("Watchdog ntfy alert sent.")

def retry_delay_seconds(attempt_count: int, retry_after: int | None = None) -> int:
    if retry_after is not None:
        return min(max(0, retry_after), PENDING_RETRY_MAX_DELAY_SECONDS)
    if attempt_count <= len(PENDING_RETRY_DELAYS_SECONDS):
        return PENDING_RETRY_DELAYS_SECONDS[attempt_count - 1]
    return PENDING_RETRY_MAX_DELAY_SECONDS

def pending_from_report(previous_seen: int | None, latest: ChapterReport, err: NotificationDeliveryError | NotificationConfigError | None = None) -> dict[str, Any]:
    now = utc_now(); now_s = now.isoformat(timespec="seconds")
    status = err.status_code if isinstance(err, NotificationDeliveryError) else None
    category = err.category if isinstance(err, NotificationDeliveryError) else ("configuration_error" if err else None)
    delay = retry_delay_seconds(1, err.retry_after_seconds if isinstance(err, NotificationDeliveryError) else None)
    return {
        "previous_seen": previous_seen,
        "first_pending_chapter": (previous_seen + 1) if previous_seen is not None else latest.chapter,
        "latest_pending_chapter": latest.chapter,
        "title": latest.title,
        "sources": source_names(latest.source),
        "url": latest.url,
        "created_at": now_s,
        "first_failure_at": now_s,
        "last_attempt_at": now_s,
        "next_retry_at": (now + timedelta(seconds=delay)).isoformat(timespec="seconds"),
        "attempt_count": 1,
        "last_error_category": category,
        "last_http_status": status,
    }

def report_from_pending(pending: dict[str, Any]) -> tuple[int | None, ChapterReport]:
    previous_seen = parse_int(pending.get("previous_seen")) if pending.get("previous_seen") is not None else None
    sources = pending.get("sources") if isinstance(pending.get("sources"), list) else []
    source = ", ".join(source_names(",".join(str(s) for s in sources)))
    return previous_seen, ChapterReport(source, int(pending["latest_pending_chapter"]), pending.get("title"), pending.get("url", ""), "pending")

def pending_due(pending: dict[str, Any]) -> bool:
    due = parse_iso_datetime(pending.get("next_retry_at"))
    return due is None or utc_now() >= due

def update_pending_after_failure(pending: dict[str, Any], err: NotificationDeliveryError | NotificationConfigError) -> None:
    now = utc_now(); attempts = (parse_int(pending.get("attempt_count")) or 0) + 1
    retry_after = err.retry_after_seconds if isinstance(err, NotificationDeliveryError) else None
    pending["attempt_count"] = attempts
    pending["last_attempt_at"] = now.isoformat(timespec="seconds")
    pending["next_retry_at"] = (now + timedelta(seconds=retry_delay_seconds(attempts, retry_after))).isoformat(timespec="seconds")
    pending["last_error_category"] = err.category if isinstance(err, NotificationDeliveryError) else "configuration_error"
    pending["last_http_status"] = err.status_code if isinstance(err, NotificationDeliveryError) else None

def merge_pending(pending: dict[str, Any] | None, previous_seen: int | None, latest: ChapterReport, err: NotificationDeliveryError | NotificationConfigError | None = None) -> dict[str, Any]:
    if pending is None:
        return pending_from_report(previous_seen, latest, err)
    if latest.chapter > int(pending["latest_pending_chapter"]):
        pending["latest_pending_chapter"] = latest.chapter
        pending["title"] = latest.title
        pending["url"] = latest.url
        pending["sources"] = source_names(latest.source)
        return pending
    if latest.chapter == int(pending["latest_pending_chapter"]):
        existing = ",".join(str(s) for s in pending.get("sources", []))
        pending["sources"] = source_names(f"{existing},{latest.source}")
    return pending
