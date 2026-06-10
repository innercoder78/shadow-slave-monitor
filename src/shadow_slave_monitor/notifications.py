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

def format_chapter_list(chapters: list[int]) -> str:
    chapter_text = [str(chapter) for chapter in chapters]
    if len(chapter_text) == 1:
        return chapter_text[0]
    if len(chapter_text) == 2:
        return f"{chapter_text[0]} and {chapter_text[1]}"
    return f"{', '.join(chapter_text[:-1])}, and {chapter_text[-1]}"

def new_chapter_numbers(previous_seen: int | None, latest_chapter: int) -> list[int]:
    if previous_seen is None or latest_chapter <= previous_seen:
        return [latest_chapter]
    if latest_chapter - previous_seen > 250:
        return [previous_seen + 1, latest_chapter]
    return list(range(previous_seen + 1, latest_chapter + 1))

def notification_title(chapter_count: int) -> str:
    return "New Shadow Slave chapter available" if chapter_count == 1 else "New Shadow Slave chapters available"

def source_general_url(source: str, fallback_url: str = "") -> str:
    first_source = source.split(",", maxsplit=1)[0].strip()
    for site in PUBLIC_SITES:
        if site.name == first_source:
            return site.url
    return fallback_url

def notification_body(previous_seen: int | None, latest: ChapterReport) -> str:
    chapters = new_chapter_numbers(previous_seen, latest.chapter)
    count = len(chapters)
    latest_chapter_line = f"Latest Chapter: {latest.chapter}" + (f" — {latest.title}" if latest.title else "") + "."
    general_source_url = source_general_url(latest.source, latest.url)
    if count == 1:
        availability_lines = ["There is 1 new chapter.", f"Chapter {latest.chapter} is now available."]
    elif previous_seen is not None and latest.chapter - previous_seen > 250:
        availability_lines = [f"There are {latest.chapter - previous_seen} new chapters.", f"Chapters {previous_seen + 1} through {latest.chapter} are now available."]
    else:
        availability_lines = [f"There are {count} new chapters.", f"Chapters {format_chapter_list(chapters)} are now available."]
    body = "\n".join([*availability_lines, latest_chapter_line, f"Source: {latest.source} [{general_source_url}]"])
    if len(body.encode("utf-8")) <= NTFY_BODY_MAX_BYTES:
        return body
    return "\n".join([
        f"There are {max(1, latest.chapter - (previous_seen or latest.chapter - 1))} new chapters.",
        f"Chapters {(previous_seen + 1) if previous_seen else latest.chapter} through {latest.chapter} are now available.",
        latest_chapter_line,
        f"Source: {latest.source} [{general_source_url}]",
    ])

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
    chapters = new_chapter_numbers(previous_seen, latest.chapter)
    _post_ntfy(topic, notification_title(len(chapters)), notification_body(previous_seen, latest))
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
        "sources": [s.strip() for s in latest.source.split(",") if s.strip()],
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
    source = ", ".join(str(s).strip() for s in sources if str(s).strip())
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
    existing = [str(s) for s in pending.get("sources", [])]
    for source in [s.strip() for s in latest.source.split(",") if s.strip()]:
        if source not in existing:
            existing.append(source)
    pending["sources"] = existing
    return pending
