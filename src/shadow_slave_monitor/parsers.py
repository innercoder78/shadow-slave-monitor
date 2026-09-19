"""Chapter source parsers."""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from shadow_slave_monitor.config import MAX_CHAPTER, MIN_CHAPTER, TITLE_MAX_LENGTH, WEBNOVEL_CATALOG_URL, SourceConfig
from shadow_slave_monitor.http_client import fetch_html, safe_exception_category
from shadow_slave_monitor.models import ChapterReport

class ParseError(RuntimeError):
    pass


LIGHTNOVELUP_BOOTSTRAP_URL = "https://lightnovelup.com/novel/shadow-slave/chapter-3173-life-goes-on/"
LIGHTNOVELUP_MAX_TRAVERSAL = 25

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


def chikari_candidate_from_href(href: str, base_url: str) -> ChapterReport | None:
    if not isinstance(href, str):
        return None
    try:
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or hostname not in {"chikari.moe", "www.chikari.moe"}
        or parsed.netloc != hostname
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None

    match = re.fullmatch(r"/novels/shadow-slave/(\d{1,5})/?", parsed.path)
    if not match:
        return None
    return ChapterReport("", int(match.group(1)), None, url)


def parse_chikari_candidates(soup: BeautifulSoup, base_url: str) -> list[ChapterReport]:
    candidates: list[ChapterReport] = []
    seen: set[tuple[int, str]] = set()
    for anchor in soup.find_all("a", href=True):
        candidate = chikari_candidate_from_href(anchor["href"], base_url)
        if candidate:
            key = (candidate.chapter, candidate.url)
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)

    return candidates


def parse_chikari_chapter_title(html: str, chapter: int) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    pattern = re.compile(rf"^\s*Chapter\s+({chapter})\b\s*[:\-–—]?\s*(.*?)\s*$", re.IGNORECASE)
    for tag_name in ("h1", "h2", "title"):
        for heading in soup.find_all(tag_name):
            match = pattern.fullmatch(heading.get_text(" ", strip=True))
            if not match:
                continue
            title_text = match.group(2)
            if tag_name == "title":
                title_text = re.sub(
                    r"\s*(?:·|-|\|)\s*chikari\.moe\s*$",
                    "",
                    title_text,
                    flags=re.IGNORECASE,
                )
            title = clean_title(title_text)
            if title and not is_non_chapter_title(title):
                return title
    return None


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


def freewebnovel_candidate_from_anchor(
    anchor: Any, base_url: str, *, allow_title_only: bool = False,
) -> ChapterReport | None:
    """Parse a chapter only when its URL and visible label independently agree."""
    href = anchor.get("href")
    if not isinstance(href, str) or not href or "%" in href:
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
    title_only = _title_only_label(text)
    if text_match and int(text_match.group(1)) == int(path_match.group(1)):
        title = clean_title(text_match.group(2))
    elif allow_title_only and title_only and not parse_chapter_text(text):
        title = title_only
    else:
        return None
    if not title or is_non_chapter_title(title):
        return None
    return ChapterReport("", int(path_match.group(1)), title, url)


def parse_freewebnovel_candidates(
    soup: BeautifulSoup, base_url: str,
    expected_chapter: int | None = None, expected_title: str | None = None,
    previous_chapter: int | None = None, previous_title: str | None = None,
) -> list[ChapterReport]:
    """Prefer canonical links scoped to FreeWebNovel's semantic latest section."""
    def candidates(nodes: list[Any], *, allow_title_only: bool = False) -> list[ChapterReport]:
        found: list[ChapterReport] = []
        seen: set[tuple[int, str]] = set()
        for node in nodes:
            anchors = [node] if getattr(node, "name", None) == "a" else node.find_all("a", href=True)
            for anchor in anchors:
                candidate = freewebnovel_candidate_from_anchor(
                    anchor, base_url, allow_title_only=allow_title_only,
                )
                if candidate and (candidate.chapter, candidate.url) not in seen:
                    seen.add((candidate.chapter, candidate.url))
                    found.append(candidate)
        return found

    markers = soup.find_all(string=re.compile(r"^\s*(?:\d+\s+)?Latest\s+Chapters\b", re.IGNORECASE))
    for marker in markers:
        heading = marker.parent
        if not heading:
            continue
        scopes = [heading, *heading.find_next_siblings(limit=1)]
        parent = heading.parent
        if (len(markers) == 1 and (
                (chapter_validity_category(expected_chapter) is None and _normalized_title(expected_title))
                or (chapter_validity_category(previous_chapter) is None and _normalized_title(previous_title)))):
            anchors: list[Any] = []
            for scope in scopes + ([parent] if parent else []):
                for anchor in ([scope] if getattr(scope, "name", None) == "a" else scope.find_all("a", href=True)):
                    if anchor not in anchors:
                        anchors.append(anchor)
            if anchors:
                first = anchors[0]
                current = freewebnovel_candidate_from_anchor(
                    first, base_url, allow_title_only=True,
                )
                if current:
                    return [current]
                href_candidate = _canonical_slug_chapter_url(
                    first.get("href"), base_url, {"freewebnovel.com", "www.freewebnovel.com"},
                    r"/novel/shadow-slave/chapter-(\d{1,5})/?",
                )
                title = _title_only_label(first.get_text(" ", strip=True))
                visible_number = parse_chapter_text(first.get_text(" ", strip=True))
                if (href_candidate and href_candidate.chapter == expected_chapter and title
                        and not visible_number
                        and _normalized_title(title) == _normalized_title(expected_title)):
                    return [ChapterReport("", expected_chapter, title, href_candidate.url)]
        nearby = candidates(scopes, allow_title_only=True)
        if nearby:
            return nearby
        if parent and getattr(parent, "name", None) not in {"body", "html", "[document]"}:
            scoped = candidates([parent], allow_title_only=True)
            if scoped:
                return scoped

    # Some templates omit a usable section wrapper. This fallback remains limited
    # to canonical Shadow Slave URLs whose visible chapter and title validate.
    return candidates([soup])


def _canonical_slug_chapter_url(
    href: Any, base_url: str, hosts: set[str], path_pattern: str
) -> ChapterReport | None:
    """Validate an untrusted chapter href without accepting URL syntax tricks."""
    if not isinstance(href, str) or not href or "%" in href:
        return None
    try:
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").casefold()
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or hostname not in hosts
        or parsed.netloc.casefold() != hostname
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    match = re.fullmatch(path_pattern, parsed.path, flags=re.IGNORECASE)
    if not match:
        return None
    chapter = int(match.group(1))
    if chapter_validity_category(chapter) is not None:
        return None
    return ChapterReport("", chapter, None, url)


def readwn_candidate_from_anchor(anchor: Any, base_url: str) -> ChapterReport | None:
    candidate = _canonical_slug_chapter_url(
        anchor.get("href"),
        base_url,
        {"readwn.org", "www.readwn.org"},
        r"/book/shadow-slave/chapter-(\d{1,5})-[a-z0-9]+(?:-[a-z0-9]+)*/?",
    )
    if not candidate:
        return None
    visible = parse_chapter_text(anchor.get_text(" ", strip=True))
    if visible and visible[0] != candidate.chapter:
        return None
    title = visible[1] if visible else None
    if title and is_non_chapter_title(title):
        title = None
    return ChapterReport("", candidate.chapter, title, candidate.url)


def parse_readwn_candidates(soup: BeautifulSoup, base_url: str) -> list[ChapterReport]:
    """Read only canonical links in Readwn's semantic latest-release area."""
    marker_pattern = r"^\s*(?:\d{1,3}\s+)?Latest\s+(?:Chapters?|Releases?)\s*:?\s*$"
    for marker in soup.find_all(string=re.compile(marker_pattern, re.IGNORECASE)):
        heading = marker.parent
        if not heading:
            continue
        scopes = [heading, *heading.find_next_siblings(limit=1)]
        parent = heading.parent
        if parent and getattr(parent, "name", None) not in {"body", "html", "[document]"}:
            scopes.append(parent)
        found: list[ChapterReport] = []
        seen: set[tuple[int, str]] = set()
        for scope in scopes:
            anchors = [scope] if getattr(scope, "name", None) == "a" else scope.find_all("a", href=True)
            for anchor in anchors:
                candidate = readwn_candidate_from_anchor(anchor, base_url)
                if candidate and (candidate.chapter, candidate.url) not in seen:
                    seen.add((candidate.chapter, candidate.url))
                    found.append(candidate)
        if found:
            return found
    return []


def _novel_live_title_slug(title: str) -> str:
    """Produce the conservative ASCII slug used to corroborate a visible title."""
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"['\N{RIGHT SINGLE QUOTATION MARK}]", "", normalized)
    return re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-")


def _normalized_title(title: str | None) -> str | None:
    """Normalize only whitespace and case for conservative title comparison."""
    if not isinstance(title, str):
        return None
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-–—\t\r\n")
    return cleaned.casefold() if cleaned and len(cleaned) <= TITLE_MAX_LENGTH else None


def _trusted_title_chapter(
    title: str | None, expected_chapter: int | None, expected_title: str | None,
    previous_chapter: int | None, previous_title: str | None,
) -> int | None:
    """Map a title only to one unambiguous trusted chapter context."""
    normalized = _normalized_title(title)
    if not normalized:
        return None
    if (chapter_validity_category(previous_chapter) is None
            and chapter_validity_category(expected_chapter) is None
            and previous_chapter > expected_chapter):
        return None
    matches = {
        chapter for chapter, trusted_title in (
            (expected_chapter, expected_title), (previous_chapter, previous_title),
        )
        if chapter_validity_category(chapter) is None
        and _normalized_title(trusted_title) == normalized
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _has_title_context(chapter: int | None, title: str | None) -> bool:
    return chapter_validity_category(chapter) is None and _normalized_title(title) is not None


def _title_only_label(text: str) -> str | None:
    match = re.fullmatch(r"\s*Chapter\s+(.+?)\s*", text, re.IGNORECASE)
    if not match or re.search(r"\b\d{1,5}\b", match.group(1)):
        return None
    title = clean_title(match.group(1))
    return title if title and not is_non_chapter_title(title) else None


def _title_slug_candidate(anchor: Any, base_url: str, hosts: set[str]) -> tuple[str, str] | None:
    """Validate the exact title-only URL shape and its visible title/slug agreement."""
    href = anchor.get("href")
    if not isinstance(href, str) or not href or "%" in href:
        return None
    try:
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
    except (TypeError, ValueError):
        return None
    if (parsed.scheme != "https" or host not in hosts or parsed.netloc.casefold() != host
            or parsed.params or parsed.query or parsed.fragment):
        return None
    path = re.fullmatch(r"/shadow-slave/chapter-([a-z0-9]+(?:-[a-z0-9]+)*)\.html", parsed.path)
    title = _title_only_label(anchor.get_text(" ", strip=True))
    if not path or not title or _novel_live_title_slug(title) != path.group(1).casefold():
        return None
    return title, url


def _title_only_report(
    anchor: Any, base_url: str, hosts: set[str], expected_chapter: int | None,
    expected_title: str | None, previous_chapter: int | None, previous_title: str | None,
) -> ChapterReport | None:
    candidate = _title_slug_candidate(anchor, base_url, hosts)
    if not candidate:
        return None
    title, url = candidate
    chapter = _trusted_title_chapter(title, expected_chapter, expected_title,
                                     previous_chapter, previous_title)
    return ChapterReport("", chapter, title, url) if chapter is not None else None


def novel_live_candidate_from_anchor(anchor: Any, base_url: str) -> ChapterReport | None:
    """Trust a release only when its canonical URL and complete visible label agree."""
    candidate = _canonical_slug_chapter_url(
        anchor.get("href"),
        base_url,
        {"novellive.com", "www.novellive.com"},
        r"/book/shadow-slave/chapter-(\d{1,5})-([a-z0-9]+(?:-[a-z0-9]+)*)",
    )
    if not candidate:
        return None

    visible_text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
    visible = re.fullmatch(r"Chapter\s+(\d{1,5})\s+(.+)", visible_text, re.IGNORECASE)
    if not visible or int(visible.group(1)) != candidate.chapter:
        return None
    title = clean_title(visible.group(2))
    if not title or is_non_chapter_title(title):
        return None

    path_match = re.fullmatch(
        r"/book/shadow-slave/chapter-\d{1,5}-([a-z0-9]+(?:-[a-z0-9]+)*)",
        urlparse(candidate.url).path,
        re.IGNORECASE,
    )
    if not path_match or _novel_live_title_slug(title) != path_match.group(1).casefold():
        return None
    return ChapterReport("", candidate.chapter, title, candidate.url)


def parse_novel_live_candidates(soup: BeautifulSoup, base_url: str) -> list[ChapterReport]:
    """Read only one unambiguous semantic latest-chapters section, or fail closed."""
    marker_pattern = r"^\s*\d{1,3}\s+Latest\s+Chapters?\s*$"
    markers = soup.find_all(string=re.compile(marker_pattern, re.IGNORECASE))
    if len(markers) != 1:
        return []
    for marker in markers:
        heading = marker.parent
        if not heading:
            continue
        scopes: list[Any] = [heading, *heading.find_next_siblings(limit=1)]
        parent = heading.parent
        if parent and getattr(parent, "name", None) not in {"body", "html", "[document]"}:
            scopes.append(parent)

        found: list[ChapterReport] = []
        seen: set[tuple[int, str]] = set()
        for scope in scopes:
            anchors = [scope] if getattr(scope, "name", None) == "a" else scope.find_all("a", href=True)
            for anchor in anchors:
                candidate = novel_live_candidate_from_anchor(anchor, base_url)
                if candidate and (candidate.chapter, candidate.url) not in seen:
                    seen.add((candidate.chapter, candidate.url))
                    found.append(candidate)
        if found:
            return found

    return []


def lightnovelup_candidate_from_href(href: Any, base_url: str) -> ChapterReport | None:
    return _canonical_slug_chapter_url(
        href,
        base_url,
        {"lightnovelup.com", "www.lightnovelup.com"},
        r"/novel/shadow-slave/chapter-(\d{1,5})-[a-z0-9]+(?:-[a-z0-9]+)*/?",
    )


def parse_lightnovelup_chapter_page(html: str, url: str) -> tuple[ChapterReport, ChapterReport | None]:
    """Validate one chapter page and its site-provided canonical Next link."""
    candidate = lightnovelup_candidate_from_href(url, url)
    if not candidate:
        raise ParseError("LightNovelUp chapter URL is not canonical")
    soup = BeautifulSoup(html, "html.parser")
    possible_titles: list[str] = []
    for heading in soup.find_all(["h1", "h2"]):
        text = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).strip()
        match = re.search(r"(?:Shadow\s+Slave\s*[-–—:]?\s*)?Chapter\s+(\d{1,5})\b(.*)", text, re.IGNORECASE)
        if not match:
            continue
        if int(match.group(1)) != candidate.chapter:
            raise ParseError("LightNovelUp chapter heading contradicts canonical URL")
        remainder = re.sub(r"^[\s:;,\.\-–—|]+", "", match.group(2))
        possible = clean_title(remainder)
        if possible and not is_non_chapter_title(possible):
            possible_titles.append(possible)
    next_markers = [
        anchor for anchor in soup.find_all("a", href=True)
        if re.fullmatch(r"\s*Next(?:\s+Chapter)?\s*", anchor.get_text(" ", strip=True), re.IGNORECASE)
    ]
    next_candidate = None
    if next_markers:
        destinations: dict[tuple[int, str], ChapterReport] = {}
        for marker in next_markers:
            validated = lightnovelup_candidate_from_href(marker.get("href"), candidate.url)
            if not validated:
                raise ParseError("LightNovelUp Next link is not canonical")
            destinations[(validated.chapter, validated.url)] = validated
        if len(destinations) != 1:
            raise ParseError("LightNovelUp chapter page has ambiguous Next navigation")
        next_candidate = next(iter(destinations.values()))
        if next_candidate.chapter != candidate.chapter + 1:
            raise ParseError("LightNovelUp Next link is not a sensible monotonic advance")
    report = ChapterReport("", candidate.chapter, possible_titles[0] if possible_titles else None, candidate.url)
    return report, next_candidate


def check_lightnovelup(site: SourceConfig, position: dict[str, Any] | None = None) -> ChapterReport:
    """Walk bounded canonical Next links from a validated cursor or bootstrap anchor."""
    start_url = LIGHTNOVELUP_BOOTSTRAP_URL
    if position is not None:
        chapter = position.get("chapter")
        url = position.get("url")
        candidate = lightnovelup_candidate_from_href(url, site.url)
        if not candidate or candidate.chapter != chapter:
            raise ParseError("LightNovelUp cursor is invalid")
        start_url = candidate.url

    current_url = start_url
    visited: set[str] = set()
    for _ in range(LIGHTNOVELUP_MAX_TRAVERSAL):
        if current_url in visited:
            raise ParseError("LightNovelUp navigation cycle detected")
        visited.add(current_url)
        report, next_candidate = parse_lightnovelup_chapter_page(fetch_html(site, current_url), current_url)
        if next_candidate is None:
            return ChapterReport(
                site.name, report.chapter, report.title, report.url,
                "LightNovelUp:canonical_navigation", report.chapter, report.url,
            )
        if next_candidate.url in visited:
            raise ParseError("LightNovelUp navigation cycle detected")
        current_url = next_candidate.url
    raise ParseError("LightNovelUp navigation traversal limit exceeded")


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


def novelfull_candidate_from_anchor(anchor: Any, base_url: str) -> ChapterReport | None:
    """Parse only canonical NovelFull Shadow Slave chapter links."""
    href = anchor.get("href")
    if not isinstance(href, str) or not href:
        return None
    try:
        url = urljoin(base_url, href)
        parsed_url = urlparse(url)
        hostname = parsed_url.hostname
    except (TypeError, ValueError):
        return None
    if (
        parsed_url.scheme != "https"
        or hostname not in {"novelfull.com", "www.novelfull.com"}
        or parsed_url.netloc.casefold() != hostname
        or parsed_url.params
        or parsed_url.query
        or parsed_url.fragment
    ):
        return None
    path_match = re.fullmatch(
        r"/shadow-slave/chapter-(\d{1,5})(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?\.html",
        parsed_url.path,
    )
    if not path_match:
        return None
    url_chapter = int(path_match.group(1))
    if chapter_validity_category(url_chapter) is not None:
        return None

    visible = parse_chapter_text(anchor.get_text(" ", strip=True))
    if visible:
        visible_chapter, title = visible
        if visible_chapter != url_chapter:
            return None
        if title and is_non_chapter_title(title):
            title = None
    else:
        title = None
    return ChapterReport("", url_chapter, title, url)


def parse_novelfull_candidates(
    soup: BeautifulSoup, base_url: str,
    expected_chapter: int | None = None, expected_title: str | None = None,
    previous_chapter: int | None = None, previous_title: str | None = None,
) -> list[ChapterReport]:
    """Return unique NovelFull candidates proved by canonical anchor URLs."""
    candidates: list[ChapterReport] = []
    seen: set[tuple[int, str]] = set()
    for anchor in soup.find_all("a", href=True):
        candidate = novelfull_candidate_from_anchor(anchor, base_url)
        if candidate and (candidate.chapter, candidate.url) not in seen:
            seen.add((candidate.chapter, candidate.url))
            candidates.append(candidate)
    if not (_has_title_context(expected_chapter, expected_title)
            or _has_title_context(previous_chapter, previous_title)):
        return candidates
    markers = soup.find_all(string=re.compile(r"^\s*Latest\s+chapters\s*$", re.IGNORECASE))
    if len(markers) != 1 or not markers[0].parent:
        return candidates
    heading = markers[0].parent
    scopes = [heading, *heading.find_next_siblings(limit=1)]
    parent = heading.parent
    if parent and getattr(parent, "name", None) not in {"body", "html", "[document]"}:
        scopes.append(parent)
    anchors: list[Any] = []
    for scope in scopes:
        for anchor in ([scope] if getattr(scope, "name", None) == "a" else scope.find_all("a", href=True)):
            if anchor not in anchors:
                anchors.append(anchor)
    if len(anchors) < 2:
        return candidates
    first = _title_slug_candidate(anchors[0], base_url, {"novelfull.com", "www.novelfull.com"})
    if first:
        first_chapter = _trusted_title_chapter(first[0], expected_chapter, expected_title,
                                               previous_chapter, previous_title)
        if first_chapter is not None and first_chapter == previous_chapter:
            return [ChapterReport("", first_chapter, first[0], first[1]), *candidates]
        predecessor = (novelfull_candidate_from_anchor(anchors[1], base_url)
                       or _title_only_report(anchors[1], base_url,
                                             {"novelfull.com", "www.novelfull.com"},
                                             expected_chapter, expected_title,
                                             previous_chapter, previous_title))
        if (first_chapter == expected_chapter and predecessor
                and predecessor.chapter == expected_chapter - 1):
            return [ChapterReport("", expected_chapter, first[0], first[1]), *candidates]
    return candidates


def _slug_html_candidate(anchor: Any, base_url: str, hosts: set[str]) -> ChapterReport | None:
    """Validate a canonical Shadow Slave slug URL and corroborate visible chapter text."""
    candidate = _canonical_slug_chapter_url(
        anchor.get("href"), base_url, hosts,
        r"/shadow-slave/chapter-(\d{1,5})-[a-z0-9]+(?:-[a-z0-9]+)*\.html",
    )
    if not candidate:
        return None
    visible_text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
    visible = parse_chapter_text(visible_text)
    if visible and visible[0] != candidate.chapter:
        return None
    title = visible[1] if visible else None
    if title and is_non_chapter_title(title):
        title = None
    return ChapterReport("", candidate.chapter, title, candidate.url)


def readnovelfull_candidate_from_anchor(anchor: Any, base_url: str) -> ChapterReport | None:
    return _slug_html_candidate(
        anchor, base_url, {"readnovelfull.com", "www.readnovelfull.com"}
    )


def parse_readnovelfull_candidates(
    soup: BeautifulSoup, base_url: str,
    expected_chapter: int | None = None, expected_title: str | None = None,
    previous_chapter: int | None = None, previous_title: str | None = None,
) -> list[ChapterReport]:
    """Trust only one unambiguous semantic latest-chapter area."""
    markers = soup.find_all(string=re.compile(r"^\s*Latest\s+chapter\s*:?\s*$", re.IGNORECASE))
    if len(markers) != 1 or not markers[0].parent:
        return []
    heading = markers[0].parent
    scopes = [heading, *heading.find_next_siblings(limit=1)]
    parent = heading.parent
    if parent and getattr(parent, "name", None) not in {"body", "html", "[document]"}:
        scopes.append(parent)
    found: dict[tuple[int, str], ChapterReport] = {}
    for scope in scopes:
        anchors = [scope] if getattr(scope, "name", None) == "a" else scope.find_all("a", href=True)
        for anchor in anchors:
            candidate = readnovelfull_candidate_from_anchor(anchor, base_url)
            if candidate:
                found[(candidate.chapter, candidate.url)] = candidate
    # Contradictory canonical entries in a singular latest area are ambiguous.
    chapters = {candidate.chapter for candidate in found.values()}
    if found:
        return list(found.values()) if len(chapters) == 1 else []
    if ((_normalized_title(expected_title) and chapter_validity_category(expected_chapter) is None)
            or (_normalized_title(previous_title) and chapter_validity_category(previous_chapter) is None)):
        title_only: dict[str, tuple[str, str]] = {}
        for scope in scopes:
            anchors = [scope] if getattr(scope, "name", None) == "a" else scope.find_all("a", href=True)
            for anchor in anchors:
                candidate = _title_slug_candidate(
                    anchor, base_url, {"readnovelfull.com", "www.readnovelfull.com"}
                )
                if candidate:
                    title_only[candidate[1]] = candidate
        if len(title_only) == 1:
            title, url = next(iter(title_only.values()))
            chapter = _trusted_title_chapter(title, expected_chapter, expected_title,
                                             previous_chapter, previous_title)
            if chapter is not None:
                return [ChapterReport("", chapter, title, url)]
    return []


def freewebnovel_net_candidate_from_anchor(anchor: Any, base_url: str) -> ChapterReport | None:
    return _slug_html_candidate(
        anchor, base_url, {"freewebnovel.net", "www.freewebnovel.net"}
    )


def parse_freewebnovel_net_candidates(
    soup: BeautifulSoup, base_url: str,
    expected_chapter: int | None = None, expected_title: str | None = None,
    previous_chapter: int | None = None, previous_title: str | None = None,
) -> list[ChapterReport]:
    """Scan only independently canonical .net chapter anchors."""
    found: dict[tuple[int, str], ChapterReport] = {}
    for anchor in soup.find_all("a", href=True):
        candidate = freewebnovel_net_candidate_from_anchor(anchor, base_url)
        if candidate:
            found[(candidate.chapter, candidate.url)] = candidate
    if not (_has_title_context(expected_chapter, expected_title)
            or _has_title_context(previous_chapter, previous_title)):
        return list(found.values())
    markers = soup.find_all(string=re.compile(
        r"^\s*\d{1,3}\s+Latest\s+Chapters?\s*(?:\[\s*Updated\s+[^\]\r\n]+\s*\])?\s*$",
        re.IGNORECASE,
    ))
    if len(markers) != 1 or not markers[0].parent:
        return list(found.values())
    heading = markers[0].parent
    parent = heading.parent
    scopes = [heading, *heading.find_next_siblings(limit=1)]
    if parent and getattr(parent, "name", None) not in {"body", "html", "[document]"}:
        scopes.append(parent)
    anchors: list[Any] = []
    for scope in scopes:
        for anchor in ([scope] if getattr(scope, "name", None) == "a" else scope.find_all("a", href=True)):
            if anchor not in anchors:
                anchors.append(anchor)
    if len(anchors) < 2:
        return list(found.values())
    first = _title_slug_candidate(
        anchors[0], base_url, {"freewebnovel.net", "www.freewebnovel.net"}
    )
    if not first:
        return list(found.values())
    first_chapter = _trusted_title_chapter(first[0], expected_chapter, expected_title,
                                           previous_chapter, previous_title)
    if first_chapter is not None and first_chapter == previous_chapter:
        return [ChapterReport("", first_chapter, first[0], first[1]), *found.values()]
    predecessor = (freewebnovel_net_candidate_from_anchor(anchors[1], base_url)
                   or _title_only_report(anchors[1], base_url,
                                         {"freewebnovel.net", "www.freewebnovel.net"},
                                         expected_chapter, expected_title,
                                         previous_chapter, previous_title))
    if (first_chapter != expected_chapter or not predecessor
            or predecessor.chapter != expected_chapter - 1):
        return list(found.values())
    return [ChapterReport("", expected_chapter, first[0], first[1]), *found.values()]


RECHAPTERS_NAMESPACE = "/book/shadow-slave-r2k2ivbd6ez4/"


def rechapters_candidate_url(anchor: Any, base_url: str) -> str | None:
    href = anchor.get("href")
    if not isinstance(href, str) or not href or "%" in href:
        return None
    try:
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
    except (TypeError, ValueError):
        return None
    if (parsed.scheme != "https" or host not in {"rechapters.com", "www.rechapters.com"}
            or parsed.netloc.casefold() != host or parsed.params or parsed.query or parsed.fragment):
        return None
    if not re.fullmatch(RECHAPTERS_NAMESPACE + r"[a-z0-9]{6,32}", parsed.path):
        return None
    return url


def rechapters_candidate_from_anchor(anchor: Any, base_url: str) -> ChapterReport | None:
    url = rechapters_candidate_url(anchor, base_url)
    if not url:
        return None
    label = re.fullmatch(
        r"\s*Ch\.?\s*(\d{1,5})\b\s*[:\-–—]?\s*(.*?)\s*",
        anchor.get_text(" ", strip=True), re.IGNORECASE,
    )
    if not label:
        return None
    chapter = int(label.group(1))
    if chapter_validity_category(chapter) is not None:
        return None
    title = clean_title(label.group(2))
    if title and is_non_chapter_title(title):
        title = None
    return ChapterReport("", chapter, title, url)


def _rechapters_chapter_list_anchors(soup: BeautifulSoup, base_url: str) -> list[Any]:
    """Return ordered, labelled chapter links from one semantic newest-first list."""
    order_markers = soup.find_all(string=re.compile(r"^\s*Newest\s+first\s*$", re.IGNORECASE))
    if len(order_markers) != 1 or not order_markers[0].parent:
        return []

    container = order_markers[0].parent
    while container and getattr(container, "name", None) not in {"body", "html", "[document]"}:
        list_markers = container.find_all(
            string=re.compile(r"^\s*Chapter\s+list\s*$", re.IGNORECASE)
        )
        canonical = [
            anchor for anchor in container.find_all("a", href=True)
            if rechapters_candidate_url(anchor, base_url)
        ]
        if len(list_markers) == 1 and canonical:
            labelled = [
                anchor for anchor in canonical
                if (_title_only_label(anchor.get_text(" ", strip=True))
                    or rechapters_candidate_from_anchor(anchor, base_url))
            ]
            return labelled
        container = container.parent
    return []


def parse_rechapters_candidates(
    soup: BeautifulSoup, base_url: str,
    expected_chapter: int | None = None, expected_title: str | None = None,
    previous_chapter: int | None = None, previous_title: str | None = None,
) -> list[ChapterReport]:
    found: dict[tuple[int, str], ChapterReport] = {}
    for anchor in soup.find_all("a", href=True):
        candidate = rechapters_candidate_from_anchor(anchor, base_url)
        if candidate:
            found[(candidate.chapter, candidate.url)] = candidate
    numbered = list(found.values())
    if not (_has_title_context(expected_chapter, expected_title)
            or _has_title_context(previous_chapter, previous_title)):
        return numbered
    anchors = _rechapters_chapter_list_anchors(soup, base_url)
    if len(anchors) < 2:
        return numbered
    first_url = rechapters_candidate_url(anchors[0], base_url)
    first_title = _title_only_label(anchors[0].get_text(" ", strip=True))
    if not first_url or not first_title:
        return numbered
    first_chapter = _trusted_title_chapter(first_title, expected_chapter, expected_title,
                                           previous_chapter, previous_title)
    if first_chapter is not None and first_chapter == previous_chapter:
        return [ChapterReport("", first_chapter, first_title, first_url), *numbered]
    predecessor = rechapters_candidate_from_anchor(anchors[1], base_url)
    if not predecessor:
        predecessor_url = rechapters_candidate_url(anchors[1], base_url)
        predecessor_title = _title_only_label(anchors[1].get_text(" ", strip=True))
        predecessor_chapter = _trusted_title_chapter(
            predecessor_title, expected_chapter, expected_title, previous_chapter, previous_title)
        if predecessor_url and predecessor_title and predecessor_chapter is not None:
            predecessor = ChapterReport("", predecessor_chapter, predecessor_title, predecessor_url)
    if (first_chapter != expected_chapter or not predecessor
            or predecessor.chapter != expected_chapter - 1):
        return numbered
    return [ChapterReport("", expected_chapter, first_title, first_url), *numbered]


def parse_rechapters_chapter_page(html: str, expected: ChapterReport) -> ChapterReport:
    soup = BeautifulSoup(html, "html.parser")
    matches: list[tuple[int, str | None]] = []
    for heading in soup.find_all(["h1", "h2"]):
        match = re.fullmatch(
            r"\s*Ch\.?\s*(\d{1,5})\b\s*[:\-–—]?\s*(.*?)\s*",
            heading.get_text(" ", strip=True), re.IGNORECASE,
        )
        if match:
            matches.append((int(match.group(1)), clean_title(match.group(2))))
    if matches:
        unique = set(matches)
        if len(unique) != 1 or matches[0][0] != expected.chapter:
            raise ParseError("ReChapters chapter heading did not confirm listing")
        page_title = matches[0][1]
    else:
        title_matches = [
            _title_only_label(heading.get_text(" ", strip=True))
            for heading in soup.find_all(["h1", "h2"])
        ]
        title_matches = [title for title in title_matches if title]
        if len({_normalized_title(title) for title in title_matches}) != 1:
            raise ParseError("ReChapters chapter heading did not confirm listing")
        page_title = title_matches[0]
    if expected.title and page_title:
        normalize = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
        if normalize(expected.title) != normalize(page_title):
            raise ParseError("ReChapters chapter title contradicted listing")
    return ChapterReport("", expected.chapter, page_title or expected.title, expected.url)


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


def parse_novelarrow_candidates(
    soup: BeautifulSoup, base_url: str,
    expected_chapter: int | None = None, expected_title: str | None = None,
    previous_chapter: int | None = None, previous_title: str | None = None,
) -> list[ChapterReport]:
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

    markers = soup.find_all(string=re.compile(r"^\s*Latest\s+chapter\s*$", re.IGNORECASE))
    for marker in markers:
        heading = marker.parent
        if not heading:
            continue
        scopes = [heading, *heading.find_next_siblings(limit=1)]
        nearby = candidates(scopes)
        if nearby:
            return nearby
        parent = heading.parent
        if parent and getattr(parent, "name", None) not in {"body", "html", "[document]"}:
            latest = candidates([parent])
            if latest:
                return latest
            scopes.append(parent)
        if (len(markers) == 1 and (
                (chapter_validity_category(expected_chapter) is None and _normalized_title(expected_title))
                or (chapter_validity_category(previous_chapter) is None and _normalized_title(previous_title)))):
            title_only: dict[str, tuple[str, str]] = {}
            for scope in scopes:
                for anchor in ([scope] if getattr(scope, "name", None) == "a" else scope.find_all("a", href=True)):
                    href = anchor.get("href")
                    if not isinstance(href, str) or not href or "%" in href:
                        continue
                    try:
                        url = urljoin(base_url, href)
                        parsed = urlparse(url)
                    except ValueError:
                        continue
                    if (parsed.scheme != "https" or (parsed.hostname or "").casefold() not in
                            {"novelarrow.com", "www.novelarrow.com"}
                            or parsed.netloc.casefold() != (parsed.hostname or "").casefold()
                            or parsed.params or parsed.query or parsed.fragment):
                        continue
                    path = re.fullmatch(
                        r"/chapter/shadow-slave/chapter-([a-z0-9]+(?:-[a-z0-9]+)*)/?",
                        parsed.path, re.IGNORECASE,
                    )
                    title = _title_only_label(anchor.get_text(" ", strip=True))
                    if (path and title and _novel_live_title_slug(title) == path.group(1).casefold()
                            and not parse_chapter_text(anchor.get_text(" ", strip=True))):
                        title_only[url] = (title, url)
            if len(title_only) == 1:
                title, url = next(iter(title_only.values()))
                chapter = _trusted_title_chapter(title, expected_chapter, expected_title,
                                                 previous_chapter, previous_title)
                if chapter is not None:
                    return [ChapterReport("", chapter, title, url)]
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
    if not isinstance(href, str) or not href or "%" in href:
        return None
    try:
        parsed_url = urlparse(href)
        host = (parsed_url.hostname or "").casefold()
    except (TypeError, ValueError):
        return None
    if (parsed_url.scheme != "https" or host not in {"telegra.ph", "www.telegra.ph"}
            or parsed_url.netloc.casefold() != host or parsed_url.params
            or parsed_url.query or parsed_url.fragment):
        return None

    path = re.fullmatch(r"/([^/]+)/?", parsed_url.path)
    if not path:
        return None
    slug = path.group(1)
    match = re.fullmatch(r"(\d{3,5})-(.+)", slug)
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


def parse_telegram_candidates(
    soup: BeautifulSoup, base_url: str,
    expected_chapter: int | None = None, expected_title: str | None = None,
    previous_chapter: int | None = None, previous_title: str | None = None,
) -> list[ChapterReport]:
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

    if (message_nodes and (_has_title_context(expected_chapter, expected_title)
                           or _has_title_context(previous_chapter, previous_title))):
        title_messages: list[tuple[int, str, str, int | None]] = []
        chapter_evidence: list[tuple[int, int | None]] = []
        for index, node in enumerate(message_nodes):
            numbered = telegram_doc_candidates_from_node(node, base_url)
            telegraphs = [parse_telegram_telegra_link(urljoin(base_url, a["href"]))
                          for a in node.find_all("a", href=True)]
            trusted_numbers = {r.chapter for r in [*numbered, *telegraphs] if r}
            if trusted_numbers:
                chapter_evidence.append((index, next(iter(trusted_numbers))
                                         if len(trusted_numbers) == 1 else None))

            docs = []
            for title_node in node.select(".tgme_widget_message_document_title"):
                text = re.sub(r"\s+", " ", title_node.get_text(" ", strip=True)).strip()
                match = re.fullmatch(r"(.+?)\.docx", text, re.IGNORECASE)
                if match and not re.search(r"\b\d{1,5}\b", match.group(1)):
                    title = clean_title(match.group(1))
                    if title:
                        docs.append(title)
            slug_titles: list[tuple[str, str]] = []
            for anchor in node.find_all("a", href=True):
                href = anchor["href"]
                if not isinstance(href, str) or "%" in href:
                    continue
                try:
                    url = urljoin(base_url, href)
                    parsed = urlparse(url)
                except ValueError:
                    continue
                host = (parsed.hostname or "").casefold()
                if (parsed.scheme != "https" or host not in {"telegra.ph", "www.telegra.ph"}
                        or parsed.netloc.casefold() != host or parsed.params or parsed.query or parsed.fragment):
                    continue
                slug = parsed.path.strip("/")
                slug = re.sub(r"-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])(?:-\d+)?$", "", slug)
                if slug and not re.match(r"^\d{3,5}-", slug):
                    slug_titles.append((clean_title(slug.replace("-", " ")) or "", url))
            if len(docs) == 1 and len(slug_titles) == 1 and not trusted_numbers:
                doc_title, (slug_title, url) = docs[0], slug_titles[0]
                if _normalized_title(doc_title) == _normalized_title(slug_title):
                    resolved = _trusted_title_chapter(
                        doc_title, expected_chapter, expected_title,
                        previous_chapter, previous_title,
                    )
                    title_messages.append((index, doc_title, url, resolved))
                    chapter_evidence.append((index, resolved))
        previous_messages = [message for message in title_messages
                             if message[3] is not None and message[3] == previous_chapter]
        newest_index = chapter_evidence[-1][0] if chapter_evidence else None
        if len(previous_messages) == 1:
            index, title, url, chapter = previous_messages[0]
            if index == newest_index:
                add(ChapterReport("", chapter, title, url))
        targets = ([message for message in title_messages if message[3] == expected_chapter]
                   if _has_title_context(expected_chapter, expected_title) else [])
        if len(targets) == 1 and targets[0][0] == newest_index:
            index, title, url, _ = targets[0]
            target_position = next(
                position for position, evidence in enumerate(chapter_evidence)
                if evidence[0] == index
            )
            predecessor = (chapter_evidence[target_position - 1][1]
                           if target_position > 0 else None)
            if predecessor == expected_chapter - 1:
                add(ChapterReport("", expected_chapter, title, url))

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


def iter_public_candidates(
    soup: BeautifulSoup, base_url: str, site_name: str = "",
    expected_chapter: int | None = None, expected_title: str | None = None,
    previous_chapter: int | None = None, previous_title: str | None = None,
) -> list[ChapterReport]:
    if site_name == "SSNovel":
        return parse_ssnovel_candidates(soup, base_url)
    if site_name == "Chikari":
        return parse_chikari_candidates(soup, base_url)
    if site_name == "Telegram":
        return parse_telegram_candidates(soup, base_url, expected_chapter, expected_title, previous_chapter, previous_title)
    if site_name == "Novel Buddy":
        return parse_novel_buddy_candidates(soup, base_url)
    if site_name == "ShadowSlave.Space":
        return parse_shadowslave_space_candidates(soup, base_url)
    if site_name == "FreeWebNovel":
        return parse_freewebnovel_candidates(soup, base_url, expected_chapter, expected_title, previous_chapter, previous_title)
    if site_name == "Readwn":
        return parse_readwn_candidates(soup, base_url)
    if site_name == "Novel Live":
        return parse_novel_live_candidates(soup, base_url)
    if site_name == "LightNovelUp":
        return []
    if site_name == "Novel Phoenix":
        return parse_novel_phoenix_candidates(soup, base_url)
    if site_name == "NovelArrow":
        return parse_novelarrow_candidates(soup, base_url, expected_chapter, expected_title, previous_chapter, previous_title)
    if site_name == "NovelFire":
        return parse_novelfire_candidates(soup, base_url)
    if site_name == "NovelFull":
        return parse_novelfull_candidates(soup, base_url, expected_chapter, expected_title, previous_chapter, previous_title)
    if site_name == "ReadNovelFull":
        return parse_readnovelfull_candidates(soup, base_url, expected_chapter, expected_title, previous_chapter, previous_title)
    if site_name == "ReChapters":
        return parse_rechapters_candidates(soup, base_url, expected_chapter, expected_title, previous_chapter, previous_title)
    if site_name == "FreeWebNovel.net":
        return parse_freewebnovel_net_candidates(soup, base_url, expected_chapter, expected_title, previous_chapter, previous_title)

    candidates: list[ChapterReport] = []
    for anchor in soup.find_all("a"):
        candidate = chapter_candidate_from_anchor(anchor, base_url)
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


def _confirm_title_only_chapter_page(html: str, expected_title: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    headings = soup.find_all(["h1", "h2"])
    if any(parse_chapter_text(heading.get_text(" ", strip=True)) for heading in headings):
        raise ParseError("chapter heading contained contradictory numeric evidence")
    titles = [
        title for title in (
            _title_only_label(heading.get_text(" ", strip=True))
            for heading in headings
        ) if title
    ]
    if (len({_normalized_title(title) for title in titles}) != 1
            or _normalized_title(titles[0]) != _normalized_title(expected_title)):
        raise ParseError("chapter heading did not confirm expected target title")
    return titles[0]


def _confirm_novelarrow_chapter_page(
    html: str, expected_chapter: int, expected_title: str,
) -> str:
    """Confirm a title-only target using only trusted headings or document title."""
    expected_normalized = _normalized_title(expected_title)
    if not expected_normalized:
        raise ParseError("NovelArrow expected title is missing")
    soup = BeautifulSoup(html, "html.parser")
    confirmations: list[str] = []
    for heading in soup.find_all(["h1", "h2"]):
        text = heading.get_text(" ", strip=True)
        numbered = parse_chapter_text(text)
        if numbered:
            if numbered[0] != expected_chapter:
                raise ParseError("NovelArrow chapter heading contradicted expected target")
            if numbered[1] and _normalized_title(numbered[1]) != expected_normalized:
                raise ParseError("NovelArrow chapter heading contradicted expected target")
            if numbered[1]:
                confirmations.append(numbered[1])
            continue
        title = _title_only_label(text)
        if title:
            confirmations.append(title)

    if soup.title:
        document_text = re.sub(r"\s+", " ", soup.title.get_text(" ", strip=True)).strip()
        numbered = parse_chapter_text(document_text)
        if numbered and numbered[0] != expected_chapter:
            raise ParseError("NovelArrow document title contradicted expected target")
        match = re.fullmatch(
            r"Shadow\s+Slave\s*/\s*Chapter\s+(.+?)\s*\|\s*Read\s+on\s+NovelArrow",
            document_text, re.IGNORECASE,
        )
        if match:
            title = clean_title(match.group(1))
            if title:
                confirmations.append(title)

    normalized = {_normalized_title(title) for title in confirmations}
    if normalized != {expected_normalized}:
        raise ParseError("NovelArrow chapter page did not unambiguously confirm expected title")
    return expected_title


def check_public_site(
    site: SourceConfig, source_position: dict[str, Any] | None = None,
    expected_chapter: int | None = None, expected_title: str | None = None,
    previous_chapter: int | None = None, previous_title: str | None = None,
) -> ChapterReport:
    logging.info("Checking %s.", site.name)
    if site.name == "LightNovelUp":
        report = check_lightnovelup(site, source_position)
        logging.info("%s reports chapter %s: %s (%s)", report.source, report.chapter,
                     report.title or "(no title)", report.url)
        return report
    soup = BeautifulSoup(fetch_html(site), "html.parser")
    candidates = filter_public_candidates(
        iter_public_candidates(soup, site.url, site.name, expected_chapter, expected_title,
                               previous_chapter, previous_title), site.name
    )

    if not candidates:
        raise ParseError(f"Could not find any chapter links on {site.name}.")
    best = max(candidates, key=lambda item: item.chapter)
    if site.name == "ReChapters":
        best = parse_rechapters_chapter_page(fetch_html(site, best.url), best)
    elif (site.name in {"NovelArrow", "NovelFull", "ReadNovelFull", "FreeWebNovel.net"}
          and expected_chapter is not None and best.chapter == expected_chapter
          and best.title and _normalized_title(best.title) == _normalized_title(expected_title)
          and parse_chapter_from_href(best.url) is None):
        page_html = fetch_html(site, best.url)
        page_title = (_confirm_novelarrow_chapter_page(page_html, best.chapter, best.title)
                      if site.name == "NovelArrow"
                      else _confirm_title_only_chapter_page(page_html, best.title))
        best = ChapterReport("", best.chapter, page_title, best.url)
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
    if report.source == "Chikari" and report.title is None:
        try:
            title = parse_chikari_chapter_title(fetch_html(site, report.url), report.chapter)
            report = ChapterReport(report.source, report.chapter, title, report.url, report.strategy)
            if title is None:
                logging.warning("Chikari title enrichment found no trustworthy matching title.")
        except Exception as exc:
            logging.warning(
                "Chikari title enrichment failed safely: category=%s type=%s",
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
