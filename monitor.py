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
        "name": "NovelFire",
        "url": "https://novelfire.net/book/shadow-slave",
    },
    {
        "name": "NovelBin",
        "url": "https://novelbin.com/b/shadow-slave",
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


def iter_public_candidates(soup: BeautifulSoup, base_url: str) -> list[ChapterReport]:
    candidates: list[ChapterReport] = []
    for anchor in soup.find_all("a"):
        text = anchor.get_text(" ", strip=True)
        href = anchor.get("href")
        parsed = parse_chapter_text(text)
        if parsed and href:
            chapter, title = parsed
            candidates.append(ChapterReport("", chapter, title, urljoin(base_url, href)))

    if candidates:
        return candidates

    # Fallback for pages that render the latest chapter as plain text.
    text = soup.get_text("\n")
    for match in re.finditer(r"\bChapter\s+(\d{1,5})\b\s*[:\-–—]?\s*([^\n\r|]*)", text, re.IGNORECASE):
        candidates.append(ChapterReport("", int(match.group(1)), clean_title(match.group(2)), base_url))
    return candidates


def check_public_site(site: dict[str, str]) -> ChapterReport:
    logging.info("Checking %s.", site["name"])
    soup = BeautifulSoup(fetch_html(site["url"]), "html.parser")
    candidates = iter_public_candidates(soup, site["url"])
    if not candidates:
        raise MonitorError(f"Could not find any chapter links on {site['name']}.")
    best = max(candidates, key=lambda item: item.chapter)
    report = ChapterReport(site["name"], best.chapter, best.title, best.url)
    logging.info("%s reports chapter %s: %s", report.source, report.chapter, report.title or "(no title)")
    return report


def check_public_sites() -> list[ChapterReport]:
    reports: list[ChapterReport] = []
    for site in PUBLIC_SITES:
        try:
            reports.append(check_public_site(site))
        except (requests.RequestException, MonitorError) as exc:
            logging.warning("%s check failed: %s", site["name"], exc)
    if not reports:
        logging.error("All public chapter site checks failed; no notification will be sent.")
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

    public_reports = check_public_sites()
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

    public_reports = check_public_sites()
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
