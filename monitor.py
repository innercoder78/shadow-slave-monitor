#!/usr/bin/env python3
"""Monitor Shadow Slave chapter availability.

The monitor treats WebNovel as the official release signal and only checks the
public chapter mirrors after WebNovel reports a chapter newer than the latest
public chapter stored in state.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

STATE_PATH = Path("state.json")
WEBNOVEL_CATALOG_URL = "https://www.webnovel.com/book/22196546206090805/catalog"
REQUEST_TIMEOUT_SECONDS = 20
WEBNOVEL_CHECK_INTERVAL = timedelta(minutes=20)
ERROR_ALERT_THROTTLE = timedelta(hours=1)
SUSPICIOUS_PUBLIC_CHAPTER_JUMP_LIMIT = 25

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PUBLIC_SITES = [
    {
        "name": "Light Novel World",
        "url": "https://lightnovelworld.org/novel/shadow-slave",
    },
    {
        "name": "NovelBin",
        "url": "https://novelbin.com/b/shadow-slave",
    },
    {
        "name": "SSNovel",
        "url": "https://ssnovel.app",
    },
    {
        "name": "NovelFull",
        "url": "https://novelfull.com/shadow-slave.html",
    },
]

INITIAL_STATE = {
    "latest_seen": None,
    "latest_title": None,
    "latest_url": None,
    "latest_webnovel": None,
    "latest_webnovel_title": None,
    "mode": "watch_webnovel",
    "target_chapter": None,
    "target_title": None,
    "target_url": None,
    "last_webnovel_check": None,
    "last_error_alerts": {},
    "updated_at": None,
}


@dataclass(frozen=True)
class ChapterReport:
    source: str
    chapter: int
    title: str | None
    url: str


class MonitorError(RuntimeError):
    """Raised when a site cannot be checked or parsed."""


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat(timespec="seconds")


def parse_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_state() -> tuple[dict[str, Any], bool]:
    if not STATE_PATH.exists():
        logging.info("state.json is missing; first setup run will initialize monitor state.")
        return INITIAL_STATE.copy(), True

    try:
        raw = STATE_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logging.warning("Could not read state.json: %s; treating as first setup.", exc)
        return INITIAL_STATE.copy(), True

    if not raw:
        logging.info("state.json is empty; first setup run will initialize monitor state.")
        return INITIAL_STATE.copy(), True

    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        logging.warning("state.json is invalid JSON: %s; treating as first setup.", exc)
        return INITIAL_STATE.copy(), True

    if loaded is None or not isinstance(loaded, dict):
        logging.info("state.json is null or not an object; first setup run will initialize monitor state.")
        return INITIAL_STATE.copy(), True

    state = INITIAL_STATE.copy()
    state.update(loaded)
    first_setup = parse_int(state.get("latest_seen")) is None and parse_int(state.get("latest_webnovel")) is None
    if first_setup:
        logging.info("state.json has no recorded chapters; first setup run will initialize monitor state.")
    return state, first_setup


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = iso_now()
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logging.info("Saved state.json.")


def fetch_html(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def clean_title(title: str | None) -> str | None:
    if not title:
        return None
    title = re.sub(r"\s+", " ", title).strip(" :-–—\t\r\n")
    return title or None


def parse_chapter_text(text: str) -> tuple[int, str | None] | None:
    patterns = [
        r"\bChapter\s+(\d{1,5})\b\s*[:\-–—]?\s*([^\n\r|]*)",
        r"\bCh(?:apter)?\.?\s*(\d{1,5})\b\s*[:\-–—]?\s*([^\n\r|]*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1)), clean_title(match.group(2))
    return None


def parse_bare_chapter_text(text: str) -> tuple[int, str | None] | None:
    """Parse SSNovel-style chapter rows such as "2986 A Memory Most Dreadful"."""
    normalized = re.sub(r"\s+", " ", text).strip()
    if re.search(r"\b(from|to)\s+chapter\b", normalized, flags=re.IGNORECASE):
        return None

    match = re.match(r"^(\d{3,5})\s+(.+)$", normalized)
    if not match:
        return None

    title = clean_title(match.group(2))
    if not title:
        return None
    if title.casefold() == "latest":
        return int(match.group(1)), None
    if is_non_chapter_title(title):
        return None
    return int(match.group(1)), title


def is_ssnovel_non_chapter_title(title: str | None) -> bool:
    if not title:
        return True
    normalized = re.sub(r"\s+", " ", title).strip().casefold()
    if is_non_chapter_title(normalized):
        return True
    return bool(
        re.fullmatch(r"chapters?", normalized)
        or re.fullmatch(r"\d+\s*(?:/|of)\s*\d+", normalized)
        or re.fullmatch(r"\d+(?:\.\d+)?\s*(?:k|m)?\s*(?:words?|views?|comments?|ratings?|votes?)", normalized)
        or re.fullmatch(r"(?:last\s+checked|updated|update|timer|page|pages?|read|latest).*", normalized)
        or re.fullmatch(r"(?:\d+\s+)?(?:seconds?|minutes?|hours?|days?)\b.*", normalized)
    )


def parse_ssnovel_leading_chapter_text(text: str) -> tuple[int, str | None] | None:
    """Parse SSNovel chapter rows, preferring the leading row number over embedded text."""
    normalized = re.sub(r"\s+", " ", text).strip()
    if re.search(r"\b(from|to)\s+chapter\b", normalized, flags=re.IGNORECASE):
        return None
    if re.match(r"^\d{1,5}\s*(?:-|–|—|to)\s*\d{1,5}\b", normalized, flags=re.IGNORECASE):
        return None

    match = re.match(r"^(\d{3,5})\s+(.+)$", normalized)
    if not match:
        return None

    title = clean_title(match.group(2))
    if is_ssnovel_non_chapter_title(title):
        return None
    return int(match.group(1)), title


def is_non_chapter_title(title: str | None) -> bool:
    if not title:
        return False
    normalized = re.sub(r"\s+", " ", title).strip().casefold()
    ui_words = (
        "chapter",
        "latest",
        "read",
        "comments",
        "comment",
        "views",
        "view",
        "rating",
        "ratings",
        "votes",
        "vote",
        "words",
        "word",
        "pages",
        "page",
        "seconds",
        "minutes",
        "hours",
        "days",
        "ago",
        "next",
        "previous",
    )
    return normalized in ui_words or bool(re.fullmatch(r"[\d\W_]+", normalized))


def parse_chapter_from_href(href: str) -> int | None:
    lowered = href.casefold()
    if "shadow-slave" not in lowered:
        return None
    patterns = [
        r"(?:chapter|chap|ch)[\-/_.]?(\d{1,5})\b",
        r"/(\d{3,5})(?:[\-/_.][a-z0-9]|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            return int(match.group(1))
    return None


def chapter_candidate_from_anchor(
    anchor: Any, base_url: str, allow_bare_text: bool = False, require_shadow_href: bool = False
) -> ChapterReport | None:
    text = anchor.get_text(" ", strip=True)
    href = anchor.get("href")
    if not href:
        return None

    parsed = parse_chapter_text(text)
    if not parsed and allow_bare_text:
        parsed = parse_bare_chapter_text(text)

    href_chapter = parse_chapter_from_href(href)
    if require_shadow_href and href_chapter is None:
        return None
    if not parsed and href_chapter is not None:
        parsed = href_chapter, None
    if not parsed:
        return None

    chapter, title = parsed
    if href_chapter is not None and href_chapter != chapter:
        return None
    return ChapterReport("", chapter, title, urljoin(base_url, href))


def ssnovel_candidate_from_anchor(anchor: Any, base_url: str) -> ChapterReport | None:
    text = anchor.get_text(" ", strip=True)
    href = anchor.get("href")
    if not href:
        return None

    parsed = parse_ssnovel_leading_chapter_text(text)
    if not parsed:
        return None

    chapter, title = parsed
    return ChapterReport("", chapter, title, urljoin(base_url, href))


def ssnovel_candidates_from_text(text: str, base_url: str) -> list[ChapterReport]:
    candidates: list[ChapterReport] = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        parsed = parse_ssnovel_leading_chapter_text(line)
        if parsed:
            chapter, title = parsed
            candidates.append(ChapterReport("", chapter, title, base_url))
    for index, line in enumerate(lines[:-1]):
        if not re.fullmatch(r"\d{3,5}", line):
            continue
        parsed = parse_ssnovel_leading_chapter_text(f"{line} {lines[index + 1]}")
        if parsed:
            chapter, title = parsed
            candidates.append(ChapterReport("", chapter, title, base_url))
    return candidates


def parse_ssnovel_candidates(soup: BeautifulSoup, base_url: str) -> list[ChapterReport]:
    candidates: list[ChapterReport] = []
    seen: set[tuple[int, str]] = set()

    for anchor in soup.find_all("a"):
        candidate = ssnovel_candidate_from_anchor(anchor, base_url)
        if not candidate:
            continue
        key = (candidate.chapter, candidate.url)
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)

    for node in soup.find_all(["article", "li", "tr", "div", "p"]):
        text = node.get_text(" ", strip=True)
        if len(text) > 200:
            continue
        parsed = parse_ssnovel_leading_chapter_text(text)
        if not parsed:
            continue
        chapter, title = parsed
        link = node.find("a", href=True)
        url = urljoin(base_url, link["href"]) if link else base_url
        key = (chapter, url)
        if key not in seen:
            seen.add(key)
            candidates.append(ChapterReport("", chapter, title, url))

    for candidate in ssnovel_candidates_from_text(soup.get_text("\n", strip=True), base_url):
        key = (candidate.chapter, candidate.url)
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)

    return candidates


def chapter_candidates_from_text(text: str, base_url: str, allow_bare_text: bool = False) -> list[ChapterReport]:
    candidates: list[ChapterReport] = []
    for line in [line.strip() for line in text.splitlines() if line.strip()]:
        parsed = parse_chapter_text(line)
        if not parsed and allow_bare_text:
            parsed = parse_bare_chapter_text(line)
        if parsed:
            chapter, title = parsed
            if title and is_non_chapter_title(title):
                continue
            candidates.append(ChapterReport("", chapter, title, base_url))
    return candidates


def find_section_nodes(soup: BeautifulSoup, heading_pattern: str) -> list[Any]:
    matches = soup.find_all(string=re.compile(heading_pattern, re.IGNORECASE))
    nodes: list[Any] = []
    for text_node in matches:
        element = text_node.parent
        if not element:
            continue

        nodes.append(element)
        nodes.extend(element.find_next_siblings(limit=1))

        parent = element.parent
        if parent and getattr(parent, "name", None) not in {"body", "html", "[document]"}:
            nodes.append(parent)
            nodes.extend(parent.find_next_siblings(limit=1))
    return nodes


def candidates_from_nodes(nodes: list[Any], base_url: str, allow_bare_text: bool = False) -> list[ChapterReport]:
    candidates: list[ChapterReport] = []
    seen: set[tuple[int, str]] = set()
    for node in nodes:
        for anchor in node.find_all("a"):
            candidate = chapter_candidate_from_anchor(anchor, base_url, allow_bare_text)
            if not candidate:
                continue
            key = (candidate.chapter, candidate.url)
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)
        for candidate in chapter_candidates_from_text(node.get_text("\n", strip=True), base_url, allow_bare_text):
            key = (candidate.chapter, candidate.url)
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)
    return candidates


def is_light_novel_world_chapter_href(href: str) -> bool:
    lowered = href.casefold()
    if "shadow-slave" not in lowered:
        return False
    return bool(
        re.search(r"/chapter/\d{1,5}/?(?:[?#].*)?$", lowered)
        or re.search(r"chapter[-_/]\d{1,5}\b", lowered)
    )


def is_light_novel_world_ui_link(anchor: Any) -> bool:
    text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip().casefold()
    css_classes = " ".join(anchor.get("class", [])).casefold()
    return bool(
        text in {"read now", "read first", "first chapter", "chapter 1", "latest chapters"}
        or "btn-read-now" in css_classes
        or re.fullmatch(r"(?:read\s+)?first(?:\s+chapter)?", text)
    )


def light_novel_world_report_from_chapter(
    chapter: int, title: str | None, base_url: str, href: str | None = None
) -> ChapterReport:
    url = urljoin(base_url, href) if href else urljoin(base_url, f"/novel/shadow-slave/chapter/{chapter}/")
    return ChapterReport("", chapter, title, url)


def light_novel_world_candidate_from_anchor(anchor: Any, base_url: str) -> ChapterReport | None:
    href = anchor.get("href")
    if not href or not is_light_novel_world_chapter_href(href):
        return None
    if is_light_novel_world_ui_link(anchor):
        return None

    href_chapter = parse_chapter_from_href(href)
    if href_chapter is None:
        return None

    text = anchor.get_text(" ", strip=True)
    parsed = parse_chapter_text(text)
    title = parsed[1] if parsed and parsed[0] == href_chapter else None
    if title and is_non_chapter_title(title):
        title = None
    return light_novel_world_report_from_chapter(href_chapter, title, base_url, href)


def parse_light_novel_world_candidates(soup: BeautifulSoup, base_url: str) -> list[ChapterReport]:
    candidates: list[ChapterReport] = []
    seen: set[tuple[int, str]] = set()

    def add(candidate: ChapterReport | None) -> None:
        if not candidate:
            return
        if candidate.chapter < 2:
            return
        key = (candidate.chapter, candidate.url)
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)

    latest_nodes: list[Any] = []
    for chapter_info in soup.select(".chapter-info"):
        parent = chapter_info.find_parent(class_=re.compile(r"(?:^|\s)(?:content-card|card-content)(?:\s|$)"))
        if parent:
            latest_nodes.append(parent)
        latest_nodes.append(chapter_info)

    latest_nodes.extend(find_section_nodes(soup, r"\b(?:latest|recent|novel)\s+chapters?\b"))

    for node in latest_nodes:
        for anchor in node.find_all("a", href=True):
            add(light_novel_world_candidate_from_anchor(anchor, base_url))

        parsed = parse_chapter_text(node.get_text("\n", strip=True))
        if parsed:
            chapter, title = parsed
            if not title or not is_non_chapter_title(title):
                add(light_novel_world_report_from_chapter(chapter, title, base_url))

    for anchor in soup.find_all("a", href=True):
        add(light_novel_world_candidate_from_anchor(anchor, base_url))

    return candidates


def parse_webnovel_latest(html: str) -> ChapterReport:
    soup = BeautifulSoup(html, "html.parser")
    lines = [line.strip() for line in soup.get_text("\n").splitlines() if line.strip()]

    for index, line in enumerate(lines):
        if "latest release" not in line.casefold():
            continue
        nearby = "\n".join(lines[index : index + 12])
        parsed = parse_chapter_text(nearby)
        if parsed:
            chapter, title = parsed
            return ChapterReport("WebNovel", chapter, title, WEBNOVEL_CATALOG_URL)

    text = soup.get_text("\n")
    marker = re.search(r"Latest\s+Release\s*[:：]?", text, flags=re.IGNORECASE)
    if marker:
        snippet = text[marker.end() : marker.end() + 1000]
        parsed = parse_chapter_text(snippet)
        if parsed:
            chapter, title = parsed
            return ChapterReport("WebNovel", chapter, title, WEBNOVEL_CATALOG_URL)

    raise MonitorError("Could not find WebNovel Latest Release chapter in catalog page.")


def check_webnovel() -> ChapterReport:
    logging.info("Checking WebNovel catalog Latest Release.")
    report = parse_webnovel_latest(fetch_html(WEBNOVEL_CATALOG_URL))
    logging.info("WebNovel reports chapter %s: %s", report.chapter, report.title or "(no title)")
    return report


def iter_public_candidates(soup: BeautifulSoup, base_url: str, site_name: str = "") -> list[ChapterReport]:
    if site_name == "SSNovel":
        return parse_ssnovel_candidates(soup, base_url)
    if site_name == "Light Novel World":
        return parse_light_novel_world_candidates(soup, base_url)

    section_patterns = {
        "FreeWebNovel": r"\b6\s+Latest\s+Chapters\b",
        "NovelFull": r"\bLatest\s+chapters\b",
        "LightNovelUp": r"\bLATEST\s+MANGA\s+RELEASES\b",
    }
    if site_name in section_patterns:
        section_candidates = candidates_from_nodes(find_section_nodes(soup, section_patterns[site_name]), base_url)
        if section_candidates:
            return section_candidates

    candidates: list[ChapterReport] = []
    require_shadow_href = site_name in {"FreeWebNovel", "NovelFull", "LightNovelUp"}
    for anchor in soup.find_all("a"):
        candidate = chapter_candidate_from_anchor(anchor, base_url, require_shadow_href=require_shadow_href)
        if candidate:
            candidates.append(candidate)

    if candidates:
        return candidates

    # Fallback for pages that render the latest chapter as plain text.
    return chapter_candidates_from_text(soup.get_text("\n"), base_url)


def parse_latest_from_chapter_page(html: str, url: str) -> ChapterReport | None:
    soup = BeautifulSoup(html, "html.parser")
    href_chapter = parse_chapter_from_href(url)
    text = soup.get_text("\n", strip=True)

    if href_chapter is not None:
        for heading in soup.find_all(["h1", "h2", "title"]):
            parsed = parse_chapter_text(heading.get_text(" ", strip=True))
            if parsed and parsed[0] == href_chapter:
                return ChapterReport("", href_chapter, parsed[1], url)
        return ChapterReport("", href_chapter, None, url)

    parsed = parse_chapter_text(text)
    if not parsed:
        return None
    chapter, title = parsed
    return ChapterReport("", chapter, title, url)


def check_public_site(site: dict[str, str]) -> ChapterReport:
    logging.info("Checking %s.", site["name"])
    soup = BeautifulSoup(fetch_html(site["url"]), "html.parser")
    candidates = iter_public_candidates(soup, site["url"], site["name"])

    if not candidates and site["name"] == "LightNovelUp":
        read_last = soup.find("a", string=re.compile(r"\bRead\s+Last\b", re.IGNORECASE))
        if read_last and read_last.get("href"):
            read_last_url = urljoin(site["url"], read_last["href"])
            logging.info("Following LightNovelUp Read Last link: %s", read_last_url)
            read_last_report = parse_latest_from_chapter_page(fetch_html(read_last_url), read_last_url)
            if read_last_report:
                candidates.append(read_last_report)

    if not candidates:
        raise MonitorError(f"Could not find any chapter links on {site['name']}.")
    best = max(candidates, key=lambda item: item.chapter)
    report = ChapterReport(site["name"], best.chapter, best.title, best.url)
    if report.source in {"SSNovel", "Light Novel World"}:
        logging.info(
            "%s reports chapter %s: %s (%s)",
            report.source,
            report.chapter,
            report.title or "(no title)",
            report.url,
        )
    else:
        logging.info("%s reports chapter %s: %s", report.source, report.chapter, report.title or "(no title)")
    return report


def check_public_sites(state: dict[str, Any]) -> list[ChapterReport]:
    reports: list[ChapterReport] = []
    baseline = public_chapter_baseline(state)
    failed_sites = 0

    for site in PUBLIC_SITES:
        try:
            report = check_public_site(site)
        except (requests.RequestException, MonitorError) as exc:
            failed_sites += 1
            logging.warning("%s check failed: %s", site["name"], exc)
            send_error_notification(
                state,
                f"public_site_failed:{site['name']}",
                error_alert_body(f"{site['name']} check failed.", str(exc)),
            )
            continue

        if baseline is not None and report.chapter > baseline + SUSPICIOUS_PUBLIC_CHAPTER_JUMP_LIMIT:
            reason = (
                f"{site['name']} reported chapter {report.chapter}, which is more than "
                f"{SUSPICIOUS_PUBLIC_CHAPTER_JUMP_LIMIT} chapters above baseline {baseline}."
            )
            logging.warning("Rejecting suspicious public chapter report: %s", reason)
            send_error_notification(
                state,
                f"suspicious_public_chapter_jump:{site['name']}",
                error_alert_body(f"Suspicious public chapter report rejected for {site['name']}.", reason),
            )
            continue

        reports.append(report)

    if not reports:
        logging.error("All public chapter site checks failed; no notification will be sent.")
        if failed_sites == len(PUBLIC_SITES):
            send_error_notification(
                state,
                "all_public_sites_failed",
                error_alert_body("All public chapter site checks failed.", "Every configured public/free site failed."),
            )
    return reports


def highest_report(reports: list[ChapterReport]) -> ChapterReport | None:
    if not reports:
        return None
    highest_chapter = max(report.chapter for report in reports)
    matching = [report for report in reports if report.chapter == highest_chapter]
    title = next((report.title for report in matching if report.title), None)
    url = matching[0].url
    sources = ", ".join(report.source for report in matching)
    return ChapterReport(sources, highest_chapter, title, url)


def availability_message(previous_seen: int | None, latest_chapter: int) -> str:
    if previous_seen is None or latest_chapter <= previous_seen + 1:
        return f"Chapter {latest_chapter} is now available."
    return f"Chapters {previous_seen + 1} through {latest_chapter} are now available."


def send_notification(previous_seen: int | None, latest: ChapterReport) -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    message = availability_message(previous_seen, latest.chapter)
    body = "\n".join(
        [
            message,
            f"Latest chapter: {latest.chapter}" + (f" - {latest.title}" if latest.title else ""),
            f"URL: {latest.url}",
            f"Reported by: {latest.source}",
        ]
    )

    if not topic:
        logging.warning("NTFY_TOPIC is missing; notification was not sent.")
        return False

    response = requests.post(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": "New Shadow Slave chapters available"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    logging.info("Sent ntfy notification for chapter %s.", latest.chapter)
    return True


def error_alert_body(summary: str, reason: str) -> str:
    return "\n".join(
        [
            "Shadow Slave monitor error:",
            summary,
            "",
            "Reason:",
            reason,
            "",
            "This alert is throttled to once per hour for this error.",
        ]
    )


def send_error_notification(state: dict[str, Any], key: str, body: str) -> bool:
    last_error_alerts = state.get("last_error_alerts")
    if not isinstance(last_error_alerts, dict):
        last_error_alerts = {}
        state["last_error_alerts"] = last_error_alerts

    last_sent = parse_iso_datetime(last_error_alerts.get(key))
    if last_sent is not None and now_utc() - last_sent < ERROR_ALERT_THROTTLE:
        logging.info("Skipping throttled error alert for %s.", key)
        return False

    topic = os.environ.get("NTFY_ERROR_TOPIC")
    if not topic:
        logging.warning("NTFY_ERROR_TOPIC is missing; error notification for %s was not sent.", key)
        return False

    try:
        response = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": "Shadow Slave monitor error"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logging.warning("Error notification for %s failed: %s", key, exc)
        return False

    last_error_alerts[key] = iso_now()
    save_state(state)
    logging.info("Sent ntfy error notification for %s.", key)
    return True


def public_chapter_baseline(state: dict[str, Any]) -> int | None:
    values = [
        parse_int(state.get("latest_seen")),
        parse_int(state.get("target_chapter")),
        parse_int(state.get("latest_webnovel")),
    ]
    available = [value for value in values if value is not None]
    if not available:
        return None
    return max(available)


def update_webnovel_state(state: dict[str, Any], report: ChapterReport) -> None:
    state["latest_webnovel"] = report.chapter
    state["latest_webnovel_title"] = report.title


def first_setup(state: dict[str, Any]) -> None:
    logging.info("Running first setup. No notification will be sent.")
    webnovel_report: ChapterReport | None = None
    public_highest: ChapterReport | None = None

    try:
        webnovel_report = check_webnovel()
        state["last_webnovel_check"] = iso_now()
        update_webnovel_state(state, webnovel_report)
    except (requests.RequestException, MonitorError) as exc:
        logging.error("WebNovel first setup check failed: %s", exc)
        send_error_notification(
            state,
            "webnovel_check_failed",
            error_alert_body("WebNovel first setup check failed.", str(exc)),
        )

    public_reports = check_public_sites(state)
    public_highest = highest_report(public_reports)
    if public_highest:
        state["latest_seen"] = public_highest.chapter
        state["latest_title"] = public_highest.title
        state["latest_url"] = public_highest.url

    public_chapter = public_highest.chapter if public_highest else None
    webnovel_chapter = webnovel_report.chapter if webnovel_report else None

    if webnovel_report and (public_chapter is None or webnovel_report.chapter > public_chapter):
        state["mode"] = "watch_free_sites"
        state["target_chapter"] = webnovel_report.chapter
        state["target_title"] = webnovel_report.title
        state["target_url"] = WEBNOVEL_CATALOG_URL
    else:
        state["mode"] = "watch_webnovel"
        state["target_chapter"] = None
        state["target_title"] = None
        state["target_url"] = None

    logging.info(
        "First setup complete. WebNovel=%s, public=%s, mode=%s.",
        webnovel_chapter,
        public_chapter,
        state["mode"],
    )
    save_state(state)


def webnovel_due(state: dict[str, Any]) -> bool:
    last_check = parse_iso_datetime(state.get("last_webnovel_check"))
    if last_check is None:
        return True
    return now_utc() - last_check >= WEBNOVEL_CHECK_INTERVAL


def run_watch_webnovel(state: dict[str, Any]) -> None:
    if not webnovel_due(state):
        logging.info("Skipping WebNovel check because fewer than 20 minutes have passed since last_webnovel_check.")
        return

    try:
        report = check_webnovel()
    except (requests.RequestException, MonitorError) as exc:
        logging.error("WebNovel check failed: %s; no notification will be sent.", exc)
        send_error_notification(
            state,
            "webnovel_check_failed",
            error_alert_body("WebNovel check failed.", str(exc)),
        )
        return

    state["last_webnovel_check"] = iso_now()
    update_webnovel_state(state, report)
    latest_seen = parse_int(state.get("latest_seen"))

    if latest_seen is not None and report.chapter <= latest_seen:
        state["mode"] = "watch_webnovel"
        logging.info("WebNovel chapter %s is not newer than latest_seen %s.", report.chapter, latest_seen)
    else:
        state["mode"] = "watch_free_sites"
        state["target_chapter"] = report.chapter
        state["target_title"] = report.title
        state["target_url"] = WEBNOVEL_CATALOG_URL
        logging.info("WebNovel is ahead; now watching public sites for chapter %s.", report.chapter)

    save_state(state)


def run_watch_free_sites(state: dict[str, Any]) -> None:
    target_chapter = parse_int(state.get("target_chapter"))
    if target_chapter is None:
        logging.warning("target_chapter is missing or invalid; switching back to watch_webnovel.")
        state["mode"] = "watch_webnovel"
        state["target_chapter"] = None
        state["target_title"] = None
        state["target_url"] = None
        save_state(state)
        return

    public_reports = check_public_sites(state)
    latest = highest_report(public_reports)
    if latest is None:
        return

    if latest.chapter < target_chapter:
        logging.info(
            "Highest public chapter %s is lower than target chapter %s; no notification will be sent.",
            latest.chapter,
            target_chapter,
        )
        return

    previous_seen = parse_int(state.get("latest_seen"))
    if not send_notification(previous_seen, latest):
        logging.info("State was not advanced because no notification was sent.")
        return

    state["latest_seen"] = latest.chapter
    state["latest_title"] = latest.title
    state["latest_url"] = latest.url
    state["target_chapter"] = None
    state["target_title"] = None
    state["target_url"] = None
    state["mode"] = "watch_webnovel"
    save_state(state)


def main() -> None:
    configure_logging()
    state, is_first_setup = load_state()
    if is_first_setup:
        first_setup(state)
        return

    mode = state.get("mode")
    if mode == "watch_free_sites":
        run_watch_free_sites(state)
    elif mode == "watch_webnovel":
        run_watch_webnovel(state)
    else:
        logging.warning("Unknown mode %r; switching to watch_webnovel without notification.", mode)
        state["mode"] = "watch_webnovel"
        save_state(state)


if __name__ == "__main__":
    main()
