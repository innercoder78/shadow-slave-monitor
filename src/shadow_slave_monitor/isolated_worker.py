"""Read-only two-source worker. Only its JSON evidence file may be written."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import platform

from bs4 import BeautifulSoup
import requests

from shadow_slave_monitor import http_client, parsers
from shadow_slave_monitor.config import PUBLIC_SITE_CONSECUTIVE_FAILURE_LIMIT, STATE_PATH
from shadow_slave_monitor.isolated_evidence import (
    EvidenceContext, EvidenceError, MAX_EVIDENCE_BYTES, isolated_sources, read_context,
    utc_stamp, validate_candidate, validate_evidence,
)
from shadow_slave_monitor.timeutil import utc_now
from shadow_slave_monitor.config import PUBLIC_SOURCE_RECOVERY_PROBE_INTERVAL, PUBLIC_SOURCE_RECOVERY_PROBE_WINDOW


def recovery_window_open(now) -> bool:
    elapsed = now.minute * 60 + now.second + now.microsecond / 1_000_000
    return elapsed % PUBLIC_SOURCE_RECOVERY_PROBE_INTERVAL.total_seconds() < PUBLIC_SOURCE_RECOVERY_PROBE_WINDOW.total_seconds()


def check_source(site, context: EvidenceContext) -> dict:
    entry = {"source": site.name, "outcome": "internal_error", "observed_at": utc_stamp(utc_now()),
             "chapter": None, "title": None, "url": None}
    if site not in isolated_sources():
        raise EvidenceError("source_configuration")
    if (context.state.get("isolated_public_source_failures", {}).get(site.name, 0)
            >= PUBLIC_SITE_CONSECUTIVE_FAILURE_LIMIT and not recovery_window_open(utc_now())):
        entry["outcome"] = "suppressed"
        return entry
    try:
        # No redirects at all: even same-host redirects can silently change the
        # series context. This uses the existing timeout/retry/size/TLS bounds.
        document = http_client.fetch_html(site, allow_redirects=False)
        metadata = getattr(document, "metadata", None)
        if metadata is not None and metadata.status != 200:
            raise http_client.HttpFetchError("unexpected_status", status=metadata.status)
    except requests.exceptions.SSLError:
        entry["outcome"] = "tls_error"
    except requests.HTTPError:
        entry["outcome"] = "http_error"
    except requests.RequestException:
        entry["outcome"] = "network_error"
    except http_client.HttpFetchError:
        entry["outcome"] = "http_policy_error"
    except Exception:
        entry["outcome"] = "internal_error"
    else:
        try:
            soup = BeautifulSoup(document, "html.parser")
            # Production requires explicit series identity for both sources.
            parsers._inactive_page_identity(soup, required=True)
            parser = {"Novel Phoenix": parsers.parse_novel_phoenix_candidates,
                      "NovelPing": parsers.parse_novelping_candidates}[site.name]
            candidates = parser(soup, site.url, reference_chapter=context.state["target_chapter"])
            if len(candidates) != 1:
                raise parsers.ParseError("chapter_data_missing")
            report = candidates[0]
            if report.source != site.name:
                raise EvidenceError("source_identity")
            # The retained latest-number parser deduplicates by number. Keep a
            # stricter production boundary for two different canonical chapter
            # URLs claiming that same release (including optional-title cases).
            marker = r"^\s*Latest\s+Release\s*:?\s*$" if site.name == "Novel Phoenix" else r"^\s*Latest\s+chapter\s*:?\s*$"
            validator = (parsers.novel_phoenix_candidate_from_anchor if site.name == "Novel Phoenix"
                         else parsers.novelping_candidate_from_anchor)
            releases = parsers._inactive_release_groups(parsers._inactive_latest_groups(soup, marker), site.url, validator)
            if any(candidate.url != report.url for candidate in releases):
                raise EvidenceError("release_url_conflict")
            validate_candidate(site, report.chapter, report.title, report.url)
            if report.chapter > min(context.state["target_chapter"], context.state["latest_webnovel"]):
                entry["outcome"] = "ahead_of_target"
            elif (report.chapter == context.state["target_chapter"] and report.title
                  and context.state.get("target_title")
                  and parsers._normalized_title(report.title) != parsers._normalized_title(context.state["target_title"])):
                raise EvidenceError("target_title_conflict")
            else:
                entry.update(outcome="candidate", chapter=report.chapter, title=report.title, url=report.url)
        except (parsers.ParseError, EvidenceError):
            entry["outcome"] = "parse_error"
        except Exception:
            entry["outcome"] = "internal_error"
    entry["observed_at"] = utc_stamp(utc_now())
    # Only allowlisted classification/source names reach logs. Never exception
    # strings, raw HTML, signed URLs, or chapter bodies.
    logging.info("Isolated source outcome: source=%s outcome=%s", site.name, entry["outcome"])
    return entry


def produce(context: EvidenceContext) -> bytes | None:
    if not context.permits(context.state):
        logging.info("Isolated checks skipped without an initialized confirmed public target.")
        return None
    sites = isolated_sources()
    with ThreadPoolExecutor(max_workers=2) as pool:
        entries = list(pool.map(lambda site: check_source(site, context), sites))
    payload = {**context.provenance(), "produced_at": utc_stamp(utc_now()), "sources": entries}
    raw = (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
    validate_evidence(raw, context, context.state, utc_now())
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise EvidenceError("payload_size")
    return raw


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        release = platform.freedesktop_os_release()
        if (os.environ.get("SHADOW_SLAVE_ISOLATED_RUNNER") != "ubuntu-26.04"
                or os.environ.get("GITHUB_EVENT_NAME") != "workflow_run"
                or release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "26.04"):
            raise EvidenceError("runner_identity")
        context = read_context(STATE_PATH, parent_run_id=os.environ.get("SHADOW_SLAVE_PARENT_RUN_ID", ""),
                               parent_attempt=os.environ.get("SHADOW_SLAVE_PARENT_RUN_ATTEMPT", ""))
        # Worker checkout is the dispatch snapshot, never a mutable branch.
        if context.checkout_sha != os.environ.get("SHADOW_SLAVE_PARENT_SHA"):
            raise EvidenceError("checkout_identity")
        raw = produce(context)
        if raw is not None:
            output = Path(os.environ["SHADOW_SLAVE_ISOLATED_OUTPUT"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(raw)
        return 0
    except Exception as exc:
        logging.error("Isolated worker setup/integrity failure: type=%s", type(exc).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
