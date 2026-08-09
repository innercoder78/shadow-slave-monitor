"""Chapter source parsers."""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from shadow_slave_monitor.config import MAX_CHAPTER, MIN_CHAPTER, TITLE_MAX_LENGTH, WEBNOVEL_CATALOG_URL, SourceConfig
from shadow_slave_monitor.http_client import fetch_html, safe_exception_category
from shadow_slave_monitor.models import ChapterReport
from shadow_slave_monitor.state_manager import valid_chapter

class ParseError(RuntimeError):
    pass

def chapter_validity_category(value: Any) -> str | None:
    if isinstance(value, bool):
        return "boolean"
    if not isinstance(value, int):
        return "non_integer"
    if value < MIN_CHAPTER:
        return "below_minimum"
    if value > MAX_CHAPTER:
        return "above_maximum"
    return None

def valid_parsed_chapter(value: Any, source: str) -> int | None:
    category = chapter_validity_category(value)
    if category is not None:
        logging.warning("Discarding invalid parsed chapter from %s: category=%s", source or "unknown", category)
        return None
    return value

def filter_public_candidates(candidates: list[ChapterReport], source: str) -> list[ChapterReport]:
    valid: list[ChapterReport] = []
    for candidate in candidates:
        chapter = valid_parsed_chapter(candidate.chapter, source)
        if chapter is None:
            continue
        valid.append(candidate)
    return valid

def require_valid_webnovel_report(report: ChapterReport) -> ChapterReport:
    if valid_parsed_chapter(report.chapter, "WebNovel") is None:
        raise ParseError("WebNovel parsed chapter is outside the trusted range")
    return report

def clean_title(title: str | None) -> str | None:
    if not title:
        return None
    title = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", title)
    title = re.sub(r"\s+", " ", title).strip(" :-–—\t\r\n")
    if len(title) > TITLE_MAX_LENGTH:
        title = title[:TITLE_MAX_LENGTH].rstrip()
    return title or None
def clean_ssnovel_title(title: str | None) -> str | None:
    title = clean_title(title)
    if not title:
        return None
    title = re.split(r"\s+\d{3,5}\s+", title, maxsplit=1)[0]
    return clean_title(title)


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

    title = clean_ssnovel_title(match.group(2))
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


def parse_novel_buddy_chapter_text(text: str) -> tuple[int, str | None] | None:
    normalized = re.sub(r"\s+", " ", text).strip()
    match = re.search(
        r"\b(?:Chapter|Ch\.)\s*(\d{1,5})\b\s*[:\-–—]?\s*(.*)",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    title = re.sub(
        r"(?:^|\s+)(?:about\s+)?\d+\s+"
        r"(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago"
        r"(?:\s+\d+)?\s*$",
        "",
        match.group(2),
        flags=re.IGNORECASE,
    )
    title = clean_title(title)
    if title and is_non_chapter_title(title):
        title = None
    return int(match.group(1)), title


def novel_buddy_candidate_from_anchor(anchor: Any, base_url: str) -> ChapterReport | None:
    href = anchor.get("href")
    if not href:
        return None

    url = urljoin(base_url, href)
    parsed_url = urlparse(url)
    if (
        parsed_url.scheme.casefold() != "https"
        or (parsed_url.hostname or "").casefold() not in {"novelbuddy.me", "www.novelbuddy.me"}
        or parsed_url.params or parsed_url.query or parsed_url.fragment
    ):
        return None
    path_match = re.fullmatch(
        r"/shadow-slave/chapter-(\d{1,5})-([a-z0-9]+(?:-[a-z0-9]+)*)/?",
        unquote(parsed_url.path), flags=re.IGNORECASE,
    )
    if not path_match:
        return None
    href_chapter = int(path_match.group(1))
    parsed = parse_novel_buddy_chapter_text(anchor.get_text(" ", strip=True))
    if not parsed:
        return ChapterReport("", href_chapter, None, url)

    chapter, title = parsed
    if href_chapter is not None and href_chapter != chapter:
        return None
    return ChapterReport("", chapter, title, url)


def parse_novel_buddy_candidates(soup: BeautifulSoup, base_url: str) -> list[ChapterReport]:
    candidates: list[ChapterReport] = []
    seen: set[tuple[int, str]] = set()

    for anchor in soup.find_all("a", href=True):
        candidate = novel_buddy_candidate_from_anchor(anchor, base_url)
        if not candidate:
            continue
        key = (candidate.chapter, candidate.url)
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)

    return candidates


def shadowslave_space_candidate_from_anchor(anchor: Any, base_url: str) -> ChapterReport | None:
    """Parse a canonical ShadowSlave.Space chapter link without trusting its UI text."""
    href = anchor.get("href")
    if not href:
        return None

    url = urljoin(base_url, href)
    parsed_url = urlparse(url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc.casefold() not in {"shadowslave.space", "www.shadowslave.space"}
        or parsed_url.params
        or parsed_url.query
        or parsed_url.fragment
    ):
        return None

    match = re.fullmatch(r"/chapters/(\d{1,5})/?", unquote(parsed_url.path))
    if not match:
        return None
    return ChapterReport("", int(match.group(1)), None, url)


def parse_shadowslave_space_candidates(soup: BeautifulSoup, base_url: str) -> list[ChapterReport]:
    """Return unique candidates proved by ShadowSlave.Space's canonical chapter URLs."""
    candidates: list[ChapterReport] = []
    seen: set[tuple[int, str]] = set()
    for anchor in soup.find_all("a", href=True):
        candidate = shadowslave_space_candidate_from_anchor(anchor, base_url)
        if candidate and (candidate.chapter, candidate.url) not in seen:
            seen.add((candidate.chapter, candidate.url))
            candidates.append(candidate)
    return candidates


def freewebnovel_candidate_from_anchor(anchor: Any, base_url: str) -> ChapterReport | None:
    """Parse a chapter only when its URL and visible label independently agree."""
    href = anchor.get("href")
    if not href:
        return None

    url = urljoin(base_url, href)
    parsed_url = urlparse(url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc.casefold() not in {"freewebnovel.com", "www.freewebnovel.com"}
        or parsed_url.params
        or parsed_url.query
        or parsed_url.fragment
    ):
        return None

    path_match = re.fullmatch(
        r"/novel/shadow-slave/chapter-(\d{1,5})/?",
        unquote(parsed_url.path),
        flags=re.IGNORECASE,
    )
    if not path_match:
        return None

    text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
    text_match = re.fullmatch(r"Chapter\s+(\d{1,5})\s+(.+)", text, flags=re.IGNORECASE)
    if not text_match or int(text_match.group(1)) != int(path_match.group(1)):
        return None

    title = clean_title(text_match.group(2))
    if not title or is_non_chapter_title(title):
        return None
    return ChapterReport("", int(path_match.group(1)), title, url)


def parse_freewebnovel_candidates(soup: BeautifulSoup, base_url: str) -> list[ChapterReport]:
    """Prefer canonical links scoped to FreeWebNovel's semantic latest section."""
    def candidates(nodes: list[Any]) -> list[ChapterReport]:
        found: list[ChapterReport] = []
        seen: set[tuple[int, str]] = set()
        for node in nodes:
            anchors = [node] if getattr(node, "name", None) == "a" else node.find_all("a", href=True)
            for anchor in anchors:
                candidate = freewebnovel_candidate_from_anchor(anchor, base_url)
                if candidate and (candidate.chapter, candidate.url) not in seen:
                    seen.add((candidate.chapter, candidate.url))
                    found.append(candidate)
        return found

    for marker in soup.find_all(string=re.compile(r"\bLatest\s+Chapters\b", re.IGNORECASE)):
        heading = marker.parent
        if not heading:
            continue
        nearby = candidates([heading, *heading.find_next_siblings(limit=1)])
        if nearby:
            return nearby
        parent = heading.parent
        if parent and getattr(parent, "name", None) not in {"body", "html", "[document]"}:
            scoped = candidates([parent])
            if scoped:
                return scoped

    # Some templates omit a usable section wrapper. This fallback remains limited
    # to canonical Shadow Slave URLs whose visible chapter and title validate.
    return candidates([soup])


def novel_phoenix_candidate_from_anchor(anchor: Any, base_url: str) -> ChapterReport | None:
    """Parse a Novel Phoenix release only when its URL and visible label agree."""
    href = anchor.get("href")
    if not href:
        return None

    url = urljoin(base_url, href)
    parsed_url = urlparse(url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc.casefold() not in {"novelphoenix.com", "www.novelphoenix.com"}
        or parsed_url.params
        or parsed_url.query
        or parsed_url.fragment
    ):
        return None

    path_match = re.fullmatch(
        r"/novel/shadow-slave/chapter-(\d{1,5})/?",
        unquote(parsed_url.path),
        flags=re.IGNORECASE,
    )
    if not path_match:
        return None

    parsed_text = parse_chapter_text(anchor.get_text(" ", strip=True))
    if not parsed_text:
        return None
    visible_chapter, title = parsed_text
    url_chapter = int(path_match.group(1))
    if visible_chapter != url_chapter or not title or is_non_chapter_title(title):
        return None
    return ChapterReport("", url_chapter, title, url)


def parse_novel_phoenix_candidates(soup: BeautifulSoup, base_url: str) -> list[ChapterReport]:
    """Trust only the canonical anchor associated with the Latest Release marker."""
    for marker in soup.find_all(string=re.compile(r"^\s*Latest\s+Release\s*:?\s*$", re.IGNORECASE)):
        element = marker.parent
        if not element:
            continue

        scopes: list[Any] = [element]
        sibling = element.find_next_sibling()
        if sibling is not None:
            scopes.append(sibling)
        parent = element.parent
        if parent and getattr(parent, "name", None) not in {"body", "html", "[document]"}:
            scopes.append(parent)

        found: list[ChapterReport] = []
        seen: set[tuple[int, str]] = set()
        for scope in scopes:
            anchors = [scope] if getattr(scope, "name", None) == "a" else scope.find_all("a", href=True)
            for anchor in anchors:
                candidate = novel_phoenix_candidate_from_anchor(anchor, base_url)
                if candidate and (candidate.chapter, candidate.url) not in seen:
                    seen.add((candidate.chapter, candidate.url))
                    found.append(candidate)
        if len(found) == 1:
            return found

    return []


def parse_shadowslave_space_chapter_title(html: str, expected_chapter: int) -> str | None:
    """Extract a title only from a heading that identifies the selected chapter."""
    soup = BeautifulSoup(html, "html.parser")
    for tag_name in ("h1", "h2", "title"):
        for heading in soup.find_all(tag_name):
            text = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).strip()
            match = re.match(
                r"^Shadow\s+Slave\s+Chapter\s+(\d{1,5})\b(.*)$",
                text,
                flags=re.IGNORECASE,
            )
            if not match or int(match.group(1)) != expected_chapter:
                continue

            remainder = re.sub(r"^[\s:;,.\-–—|]+", "", match.group(2))
            repeated = re.match(
                rf"^Chapter\s+{expected_chapter}\b(.*)$",
                remainder,
                flags=re.IGNORECASE,
            )
            if repeated:
                remainder = re.sub(r"^[\s:;,.\-–—|]+", "", repeated.group(1))
                remainder = re.sub(
                    rf"^{expected_chapter}\s*:\s*",
                    "",
                    remainder,
                    count=1,
                )

            title = clean_title(remainder)
            if not title or is_non_chapter_title(title):
                continue
            normalized = title.casefold()
            if re.fullmatch(rf"(?:shadow\s+slave\s+)?chapter\s+{expected_chapter}", normalized):
                continue
            if re.fullmatch(r"(?:read(?:\s+(?:online|now))?|latest|new|home|next|previous)", normalized):
                continue
            return title
    return None


def _strip_novelfire_metadata(value: str) -> str | None:
    value = re.sub(
        r"\s+Updated\s+(?:about\s+)?\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago\s*$",
        "", value, flags=re.IGNORECASE,
    )
    return clean_title(value)


def novelfire_candidate_from_anchor(anchor: Any, base_url: str) -> ChapterReport | None:
    href = anchor.get("href")
    if not href:
        return None
    url = urljoin(base_url, href)
    parsed_url = urlparse(url)
    if (parsed_url.scheme.casefold() != "https" or (parsed_url.hostname or "").casefold() not in
            {"novelfire.net", "www.novelfire.net"} or parsed_url.params or parsed_url.query or parsed_url.fragment):
        return None
    path = unquote(parsed_url.path)
    chapter_path = re.fullmatch(r"/book/shadow-slave/chapter-(\d{1,5})/?", path, flags=re.IGNORECASE)
    chapters_path = re.fullmatch(r"/book/shadow-slave/chapters/?", path, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
    match = re.search(r"\bChapter\s+(\d{1,5})\b\s*[:\-–—]?\s*(.*)", text, flags=re.IGNORECASE)
    if not match or not (chapter_path or chapters_path):
        return None
    visible = int(match.group(1))
    if chapter_path and int(chapter_path.group(1)) != visible:
        return None
    title = _strip_novelfire_metadata(match.group(2))
    return ChapterReport("", visible, title, url)


def parse_novelfire_candidates(soup: BeautifulSoup, base_url: str) -> list[ChapterReport]:
    candidates = [
        candidate for anchor in soup.find_all("a", href=True)
        if (candidate := novelfire_candidate_from_anchor(anchor, base_url)) is not None
    ]
    individual = [candidate for candidate in candidates if "/chapter-" in urlparse(candidate.url).path]
    if individual:
        by_chapter = {candidate.chapter for candidate in candidates}
        if len(by_chapter) > 1:
            logging.warning("NovelFire pages disagreed: chapters=%s,%s", min(by_chapter), max(by_chapter))
        highest = max(candidate.chapter for candidate in candidates)
        matching_individual = [candidate for candidate in individual if candidate.chapter == highest]
        if matching_individual:
            return matching_individual
    return candidates


def novelarrow_chapter_path(href: str, base_url: str) -> tuple[int, str, str] | None:
    """Return the chapter, title slug, and URL for a canonical NovelArrow link."""
    url = urljoin(base_url, href)
    parsed_url = urlparse(url)
    if parsed_url.scheme != "https" or parsed_url.netloc.casefold() not in {"novelarrow.com", "www.novelarrow.com"}:
        return None
    if parsed_url.params or parsed_url.query or parsed_url.fragment:
        return None

    match = re.fullmatch(
        r"/chapter/shadow-slave/chapter-(\d{1,5})-([a-z0-9]+(?:-[a-z0-9]+)*)/?",
        unquote(parsed_url.path),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return int(match.group(1)), match.group(2).casefold(), url


def novelarrow_title(text: str, slug: str) -> tuple[int, str] | None:
    normalized = re.sub(r"\s+", " ", text).strip()
    match = re.fullmatch(r"C(\d{1,5})\s+(.+)", normalized, flags=re.IGNORECASE)
    if not match:
        return None

    title = clean_title(match.group(2))
    if not title or is_non_chapter_title(title):
        return None

    def title_slug(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")

    # Some older rows append a numeric site value after the displayed title. Only
    # remove it when the canonical URL proves that it is absent from the title.
    without_metadata = re.fullmatch(r"(.+?)\s+(\d+)", title)
    if title_slug(title) != slug and without_metadata:
        possible_title = clean_title(without_metadata.group(1))
        if possible_title and title_slug(possible_title) == slug:
            title = possible_title
    return int(match.group(1)), title


def novelarrow_candidate_from_anchor(anchor: Any, base_url: str) -> ChapterReport | None:
    href = anchor.get("href")
    if not href:
        return None
    path = novelarrow_chapter_path(href, base_url)
    if not path:
        return None
    href_chapter, slug, url = path
    visible = novelarrow_title(anchor.get_text(" ", strip=True), slug)
    if not visible or visible[0] != href_chapter:
        return None
    return ChapterReport("", href_chapter, visible[1], url)


def parse_novelarrow_candidates(soup: BeautifulSoup, base_url: str) -> list[ChapterReport]:
    """Parse strict Shadow Slave chapter links, preferring the Latest chapter section."""
    def candidates(nodes: list[Any]) -> list[ChapterReport]:
        found: list[ChapterReport] = []
        seen: set[tuple[int, str]] = set()
        for node in nodes:
            anchors = [node] if getattr(node, "name", None) == "a" else node.find_all("a", href=True)
            for anchor in anchors:
                candidate = novelarrow_candidate_from_anchor(anchor, base_url)
                if candidate and (candidate.chapter, candidate.url) not in seen:
                    seen.add((candidate.chapter, candidate.url))
                    found.append(candidate)
        return found

    for marker in soup.find_all(string=re.compile(r"^\s*Latest\s+chapter\s*$", re.IGNORECASE)):
        heading = marker.parent
        if not heading:
            continue
        nearby = candidates([heading, *heading.find_next_siblings(limit=1)])
        if nearby:
            return nearby
        parent = heading.parent
        if parent and getattr(parent, "name", None) not in {"body", "html", "[document]"}:
            latest = candidates([parent])
            if latest:
                return latest
    return candidates([soup])


def parse_telegram_doc_title(text: str) -> tuple[int, str | None] | None:
    normalized = re.sub(r"\s+", " ", text).strip()
    match = re.search(r"\b(\d{3,5})\s+(.+?)\.docx\b", normalized, flags=re.IGNORECASE)
    if not match:
        return None

    title = clean_title(match.group(2))
    if not title or is_non_chapter_title(title):
        return None
    return int(match.group(1)), title


def parse_telegram_telegra_link(href: str) -> ChapterReport | None:
    parsed_url = urlparse(href)
    if parsed_url.netloc.casefold() not in {"telegra.ph", "www.telegra.ph"}:
        return None

    slug = unquote(parsed_url.path.rstrip("/").rsplit("/", maxsplit=1)[-1])
    match = re.match(r"^(\d{3,5})-(.+)$", slug)
    if not match:
        return None

    month = r"(?:0?[1-9]|1[0-2])"
    day = r"(?:0?[1-9]|[12]\d|3[01])"
    title_slug = re.sub(rf"-{month}-{day}(?:-\d+)?$", "", match.group(2))
    title = clean_title(title_slug.replace("-", " "))
    if not title or is_non_chapter_title(title):
        return None
    return ChapterReport("", int(match.group(1)), title, href)


def closest_href(node: Any, base_url: str) -> str | None:
    if getattr(node, "name", None) == "a" and node.get("href"):
        return urljoin(base_url, node["href"])

    child_link = node.find("a", href=True) if hasattr(node, "find") else None
    if child_link:
        return urljoin(base_url, child_link["href"])

    parent_link = node.find_parent("a", href=True) if hasattr(node, "find_parent") else None
    if parent_link:
        return urljoin(base_url, parent_link["href"])
    return None


def telegram_doc_candidates_from_node(node: Any, base_url: str) -> list[ChapterReport]:
    candidates: list[ChapterReport] = []
    seen_chapters: set[int] = set()

    for title_node in node.select(".tgme_widget_message_document_title") if hasattr(node, "select") else []:
        parsed = parse_telegram_doc_title(title_node.get_text(" ", strip=True))
        if not parsed:
            continue
        chapter, title = parsed
        if chapter in seen_chapters:
            continue
        candidates.append(ChapterReport("", chapter, title, closest_href(title_node, base_url) or base_url))
        seen_chapters.add(chapter)

    for document_node in node.select(".tgme_widget_message_document_wrap") if hasattr(node, "select") else []:
        parsed = parse_telegram_doc_title(document_node.get_text(" ", strip=True))
        if not parsed:
            continue
        chapter, title = parsed
        if chapter in seen_chapters:
            continue
        candidates.append(ChapterReport("", chapter, title, closest_href(document_node, base_url) or base_url))
        seen_chapters.add(chapter)

    for match in re.finditer(r"\b(\d{3,5})\s+(.+?)\.docx\b", node.get_text("\n", strip=True), flags=re.IGNORECASE):
        parsed = parse_telegram_doc_title(match.group(0))
        if not parsed:
            continue
        chapter, title = parsed
        if chapter in seen_chapters:
            continue
        candidates.append(ChapterReport("", chapter, title, base_url))
        seen_chapters.add(chapter)

    return candidates


def parse_telegram_candidates(soup: BeautifulSoup, base_url: str) -> list[ChapterReport]:
    candidates: list[ChapterReport] = []
    seen: set[tuple[int, str]] = set()

    def add(candidate: ChapterReport | None) -> None:
        if not candidate:
            return
        key = (candidate.chapter, candidate.url)
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)

    message_nodes = soup.select(".tgme_widget_message")
    nodes: list[Any] = list(message_nodes) if message_nodes else [soup]

    for node in nodes:
        telegra_reports: list[ChapterReport] = []
        for anchor in node.find_all("a", href=True):
            href = urljoin(base_url, anchor["href"])
            report = parse_telegram_telegra_link(href)
            if report:
                telegra_reports.append(report)

        doc_candidates = telegram_doc_candidates_from_node(node, base_url)
        doc_by_chapter = {report.chapter: report for report in doc_candidates}
        telegra_by_chapter = {report.chapter: report for report in telegra_reports}
        for report in telegra_reports:
            doc_candidate = doc_by_chapter.get(report.chapter)
            if doc_candidate:
                report = ChapterReport("", report.chapter, doc_candidate.title, report.url)
            add(report)

        for doc_candidate in doc_candidates:
            if doc_candidate.chapter not in telegra_by_chapter:
                add(doc_candidate)

    if message_nodes and not candidates:
        for anchor in soup.find_all("a", href=True):
            add(parse_telegram_telegra_link(urljoin(base_url, anchor["href"])))
        for doc_candidate in telegram_doc_candidates_from_node(soup, base_url):
            add(doc_candidate)

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
            return require_valid_webnovel_report(ChapterReport("WebNovel", chapter, title, WEBNOVEL_CATALOG_URL, "webnovel_latest_release"))

    text = soup.get_text("\n")
    marker = re.search(r"Latest\s+Release\s*[:：]?", text, flags=re.IGNORECASE)
    if marker:
        snippet = text[marker.end() : marker.end() + 1000]
        parsed = parse_chapter_text(snippet)
        if parsed:
            chapter, title = parsed
            return require_valid_webnovel_report(ChapterReport("WebNovel", chapter, title, WEBNOVEL_CATALOG_URL, "webnovel_latest_release"))

    raise ParseError("Could not find WebNovel Latest Release chapter in catalog page.")


def check_webnovel(source: SourceConfig) -> ChapterReport:
    logging.info("Checking WebNovel catalog Latest Release.")
    report = parse_webnovel_latest(fetch_html(source))
    logging.info("WebNovel reports chapter %s: %s", report.chapter, report.title or "(no title)")
    return report


def iter_public_candidates(soup: BeautifulSoup, base_url: str, site_name: str = "") -> list[ChapterReport]:
    if site_name == "SSNovel":
        return parse_ssnovel_candidates(soup, base_url)
    if site_name == "Light Novel World":
        return parse_light_novel_world_candidates(soup, base_url)
    if site_name == "Telegram":
        return parse_telegram_candidates(soup, base_url)
    if site_name == "Novel Buddy":
        return parse_novel_buddy_candidates(soup, base_url)
    if site_name == "ShadowSlave.Space":
        return parse_shadowslave_space_candidates(soup, base_url)
    if site_name == "FreeWebNovel":
        return parse_freewebnovel_candidates(soup, base_url)
    if site_name == "Novel Phoenix":
        return parse_novel_phoenix_candidates(soup, base_url)
    if site_name == "NovelArrow":
        return parse_novelarrow_candidates(soup, base_url)
    if site_name == "NovelFire":
        return parse_novelfire_candidates(soup, base_url)

    section_patterns = {
        "NovelFull": r"\bLatest\s+chapters\b",
        "LightNovelUp": r"\bLATEST\s+MANGA\s+RELEASES\b",
    }
    if site_name in section_patterns:
        section_candidates = candidates_from_nodes(find_section_nodes(soup, section_patterns[site_name]), base_url)
        if section_candidates:
            return section_candidates

    candidates: list[ChapterReport] = []
    require_shadow_href = site_name in {"NovelFull", "LightNovelUp"}
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


def check_public_site(site: SourceConfig) -> ChapterReport:
    logging.info("Checking %s.", site.name)
    soup = BeautifulSoup(fetch_html(site), "html.parser")
    candidates = filter_public_candidates(iter_public_candidates(soup, site.url, site.name), site.name)

    if not candidates and site.name == "LightNovelUp":
        read_last = soup.find("a", string=re.compile(r"\bRead\s+Last\b", re.IGNORECASE))
        if read_last and read_last.get("href"):
            read_last_url = urljoin(site.url, read_last["href"])
            logging.info("Following LightNovelUp Read Last link: %s", read_last_url)
            read_last_report = parse_latest_from_chapter_page(fetch_html(site, read_last_url), read_last_url)
            if read_last_report and valid_parsed_chapter(read_last_report.chapter, site.name) is not None:
                candidates.append(read_last_report)

    if not candidates:
        raise ParseError(f"Could not find any chapter links on {site.name}.")
    best = max(candidates, key=lambda item: item.chapter)
    report = ChapterReport(site.name, best.chapter, best.title, best.url, f"{site.name}:latest_candidate")
    if report.source == "ShadowSlave.Space" and report.title is None:
        try:
            title = parse_shadowslave_space_chapter_title(fetch_html(site, report.url), report.chapter)
            report = ChapterReport(report.source, report.chapter, title, report.url, report.strategy)
        except Exception as exc:
            logging.warning(
                "ShadowSlave.Space title enrichment failed safely: category=%s type=%s",
                safe_exception_category(exc),
                type(exc).__name__,
            )
    logging.info(
        "%s reports chapter %s: %s (%s)",
        report.source,
        report.chapter,
        report.title or "(no title)",
        report.url,
    )
    return report
