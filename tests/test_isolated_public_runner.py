"""Offline integration contracts; synthetic pages do not prove hosted operation."""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import zipfile

import requests

from shadow_slave_monitor import (
    isolated_evidence as contract, isolated_handoff as handoff,
    isolated_worker as worker, monitor, parsers, state_manager, watchdog,
)
from shadow_slave_monitor.config import PUBLIC_SITES, PUBLIC_SITE_ORDER
from shadow_slave_monitor.diagnostics import HtmlDocument, ResponseMetadata
from shadow_slave_monitor.http_client import HttpFetchError
from shadow_slave_monitor.models import ChapterReport, Health, RunResult
from shadow_slave_monitor.notifications import NotificationDeliveryError, merge_pending, notification_body

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 27, 12, 1, tzinfo=timezone.utc)
NAMES = ("Novel Phoenix", "NovelPing")
EXISTING = ["Chikari", "Telegram", "Novel Buddy", "ShadowSlave.Space", "FreeWebNovel",
            "SSNovel", "NovelFull", "LightNovelUp", "ReadNovelFull", "FreeWebNovel.net", "Rentry"]


def state():
    value = state_manager.initial_state()
    value.update(latest_seen=3226, latest_webnovel=3227, latest_title="Previous",
                 latest_url=PUBLIC_SITES[0].url, latest_webnovel_title="A New Dawn",
                 mode="watch_free_sites", target_chapter=3227, target_title="A New Dawn",
                 target_url="https://www.webnovel.com/book/confirmed")
    return value


def context(value=None):
    return contract.EvidenceContext("innercoder78/shadow-slave-monitor", "a" * 40,
                                    "1234", "2", "b" * 64, copy.deepcopy(value or state()))


def chapter_url(name, number=3227):
    return (f"https://novelphoenix.com/novel/shadow-slave/chapter-{number}" if name == NAMES[0]
            else f"https://novelping.com/book/shadow-slave/chapter-{number}-a-new-dawn")


def page(name, *, number=3227, title="A New Dawn", href=None, identity="Shadow Slave"):
    marker = "Latest Release:" if name == NAMES[0] else "Latest chapter:"
    label = f"Chapter {number}" + (f": {title}" if title is not None else "")
    return (f"<h1>{identity}</h1><section><b>{marker}</b>"
            f'<a href="{href or chapter_url(name, number)}">{label}</a></section>')


def entry(name, outcome="candidate", number=3227, title="A New Dawn"):
    return {"source": name, "outcome": outcome, "observed_at": contract.utc_stamp(NOW),
            "chapter": number if outcome == "candidate" else None,
            "title": title if outcome == "candidate" else None,
            "url": chapter_url(name, number) if outcome == "candidate" else None}


def payload(ctx=None):
    return {**(ctx or context()).provenance(), "produced_at": contract.utc_stamp(NOW),
            "sources": [entry(name) for name in NAMES]}


def encode(value):
    return json.dumps(value).encode()


def zipped(raw, filename="evidence.json"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, raw)
    return buffer.getvalue()


class OfflineCase(unittest.TestCase):
    def setUp(self):
        # Every new test has a last-resort network/notification guard, including
        # tests whose higher-level transport or notification call is mocked.
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("live HTTP forbidden")))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("live socket forbidden")))
        self.enterContext(patch("socket.getaddrinfo", side_effect=AssertionError("live DNS forbidden")))
        self.enterContext(patch("shadow_slave_monitor.notifications._post_ntfy", side_effect=AssertionError("live notification forbidden")))
        self.enterContext(patch.dict(os.environ, {"SHADOW_SLAVE_ISOLATED_EVIDENCE": "true"}))


class EvidenceContractTests(OfflineCase):
    def validate(self, data, current=None, now=NOW):
        return contract.validate_evidence(encode(data), context(), current or state(), now)

    def test_current_run_evidence_and_optional_title_are_valid(self):
        data = payload()
        data["sources"][1]["title"] = None
        self.assertEqual(self.validate(data), data["sources"])

    def test_provenance_and_schema_must_match_exactly(self):
        for key, value in {
            "schema_version": 2, "repository": "someone/else", "checkout_sha": "c" * 40,
            "workflow_run_id": "1235", "workflow_run_attempt": "1", "base_sha256": "c" * 64,
            "mode": "watch_webnovel", "target_chapter": 3228, "latest_webnovel": 3228,
        }.items():
            with self.subTest(field=key):
                data = payload()
                data[key] = value
                with self.assertRaises(contract.EvidenceError):
                    self.validate(data)
        for key in payload():
            data = payload()
            del data[key]
            with self.subTest(missing=key), self.assertRaises(contract.EvidenceError):
                self.validate(data)
        for key in ("html", "notification_url", "validated", "extra"):
            data = payload()
            data[key] = "untrusted"
            with self.assertRaises(contract.EvidenceError):
                self.validate(data)
        data = payload()
        data["schema_version"] = True
        with self.assertRaises(contract.EvidenceError):
            self.validate(data)

    def test_state_advance_mode_authority_and_delivered_target_are_rejected(self):
        for changes in ({"mode": "watch_webnovel"}, {"target_chapter": 3228},
                        {"latest_webnovel": 3228}, {"latest_seen": 3227},
                        {"target_title": "Changed title"}, {"latest_webnovel": None}):
            current = state()
            current.update(changes)
            with self.subTest(changes=changes), self.assertRaises(contract.EvidenceError):
                self.validate(payload(), current)

    def test_future_expired_and_invalid_observation_times_fail_closed(self):
        for field in ("produced_at", "observed_at"):
            for offset in (6, -601):
                data = payload()
                destination = data if field == "produced_at" else data["sources"][0]
                destination[field] = contract.utc_stamp(NOW + timedelta(seconds=offset))
                with self.subTest(field=field, offset=offset), self.assertRaises(contract.EvidenceError):
                    self.validate(data)
        for bad in (None, 1, "2026-99-99T12:00:00+00:00", "2026-10-27T12:01:00", "yesterday"):
            data = payload()
            data["produced_at"] = bad
            with self.assertRaises(contract.EvidenceError):
                self.validate(data)

    def test_unknown_duplicate_conflicting_and_missing_sources_fail_closed(self):
        for sources in ([entry(NAMES[0])], [entry(NAMES[0])] * 2,
                        [entry(NAMES[0]), {**entry(NAMES[1]), "source": "WebNovel"}],
                        [entry(NAMES[0]), entry(NAMES[0], number=3226)], []):
            data = payload()
            data["sources"] = sources
            with self.assertRaises(contract.EvidenceError):
                self.validate(data)

    def test_malformed_oversized_duplicate_json_and_complex_payloads_fail(self):
        samples = [b"", b"{", b"null", b"[]", b"{}" * 9000, b'{"x":1,"x":2}',
                   b'{"x":NaN}', b"[" * 2000 + b"]" * 2000, b"\xff"]
        for raw in samples:
            with self.subTest(sample=raw[:30]), self.assertRaises(contract.EvidenceError):
                contract.validate_evidence(raw, context(), state(), NOW)

    def test_numbers_urls_and_title_consistency_are_independently_checked(self):
        for name in NAMES:
            for changes in (
                {"chapter": True}, {"chapter": "3227"}, {"chapter": 0}, {"chapter": 10001},
                {"chapter": 3226}, {"url": "https://evil.example/chapter-3227"},
                {"url": chapter_url(name).replace("https:", "http:")},
                {"url": chapter_url(name) + "?next=1"}, {"url": chapter_url(name) + "#x"},
                {"url": chapter_url(name).replace(".com/", ".com:443/")},
                {"url": chapter_url(name).replace("/shadow-slave/", "/other-series/")},
                {"url": chapter_url(name).replace("3227", "03227")},
                {"url": chapter_url(name).replace("/chapter-3227", "/chapters")},
                {"title": "Chapter 3228: Contradiction"}, {"title": "Different Title"},
                {"title": "Views 999999"}, {"title": "x" * 181}, {"title": "bad\nTitle"},
                {"validated": True}, {"outcome": "HTTP_200"}, {"outcome": []},
            ):
                data = payload()
                index = NAMES.index(name)
                data["sources"][index].update(changes)
                with self.subTest(name=name, changes=changes), self.assertRaises(contract.EvidenceError):
                    self.validate(data)

    def test_remote_chapter_above_authority_is_never_a_candidate(self):
        for name in NAMES:
            data = payload()
            data["sources"][NAMES.index(name)] = entry(name, number=3228)
            with self.assertRaises(contract.EvidenceError):
                self.validate(data)
        data = payload()
        data["sources"][0] = entry(NAMES[0], "http_error")
        data["sources"][0]["chapter"] = 3227
        with self.assertRaises(contract.EvidenceError):
            self.validate(data)

    def test_context_reads_one_snapshot_and_validates_current_checkout_provenance(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            raw = encode(state())
            path.write_bytes(raw)
            with patch.dict(os.environ, {"GITHUB_REPOSITORY": "innercoder78/shadow-slave-monitor",
                                         "GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "2"}), \
                 patch.object(contract.subprocess, "check_output", return_value="a" * 40 + "\n"):
                ctx = contract.read_context(path)
                self.assertEqual(ctx.base_sha256, hashlib.sha256(raw).hexdigest())
                self.assertEqual(ctx.provenance(), context().provenance() | {"base_sha256": ctx.base_sha256})
                parent = contract.read_context(path, parent_run_id="999", parent_attempt="3")
                self.assertEqual((parent.workflow_run_id, parent.workflow_run_attempt), ("999", "3"))
                with self.assertRaises(contract.EvidenceError):
                    contract.read_context(path, parent_run_id="", parent_attempt="1")


class WorkerTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(worker, "utc_now", return_value=NOW))

    def test_only_exact_two_configurations_can_execute(self):
        self.assertEqual(tuple(site.name for site in contract.isolated_sources()), NAMES)
        with patch.object(worker.http_client, "fetch_html") as fetch:
            for site in (*[s for s in PUBLIC_SITES if s.enabled],
                         replace(contract.isolated_sources()[0], enabled=True),
                         replace(contract.isolated_sources()[0], url="https://evil.example/")):
                with self.assertRaises(contract.EvidenceError):
                    worker.check_source(site, context())
        fetch.assert_not_called()
        with patch.object(contract, "PUBLIC_SITES", tuple(s for s in PUBLIC_SITES if s.name != NAMES[1])):
            with self.assertRaises(contract.EvidenceError):
                contract.isolated_sources()

    def test_canonical_success_uses_one_listing_request_per_source_without_details(self):
        with patch.object(worker.http_client, "fetch_html", side_effect=lambda site, **kw: page(site.name)) as fetch:
            raw = worker.produce(context())
        result = contract.validate_evidence(raw, context(), state(), NOW)
        self.assertEqual([item["outcome"] for item in result], ["candidate", "candidate"])
        self.assertCountEqual([call.args[0].name for call in fetch.call_args_list], NAMES)
        self.assertTrue(all(call.kwargs == {"allow_redirects": False, "max_attempts": 1} for call in fetch.call_args_list))

    def test_missing_optional_title_survives_without_any_detail_fetch(self):
        site = contract.isolated_sources()[1]
        with patch.object(worker.http_client, "fetch_html", return_value=page(site.name, title=None)) as fetch:
            result = worker.check_source(site, context())
        self.assertEqual((result["outcome"], result["title"]), ("candidate", None))
        fetch.assert_called_once()
        site = contract.isolated_sources()[0]
        with patch.object(worker.http_client, "fetch_html", return_value=page(site.name, title=None)):
            self.assertEqual(worker.check_source(site, context())["outcome"], "parse_error")

    def test_http_network_tls_and_parser_failures_are_separate_with_partial_success(self):
        errors = [(requests.Timeout(), "network_error"), (requests.ConnectionError(), "network_error"),
                  (requests.exceptions.SSLError(), "tls_error"),
                  (HttpFetchError("redirect_not_allowed"), "http_policy_error"),
                  (HttpFetchError("response_too_large"), "http_policy_error"),
                  (RuntimeError("unfiltered sensitive details"), "internal_error")]
        for status in (403, 404, 429, 500, 503):
            response = requests.Response()
            response.status_code = status
            errors.append((requests.HTTPError(response=response), "http_error"))
        for error, expected in errors:
            def fetch(site, **kwargs):
                if site.name == NAMES[0]:
                    raise error
                return page(site.name)
            with self.subTest(error=type(error).__name__, expected=expected), \
                 patch.object(worker.http_client, "fetch_html", side_effect=fetch):
                raw = worker.produce(context())
            entries = contract.validate_evidence(raw, context(), state(), NOW)
            self.assertEqual([item["outcome"] for item in entries], [expected, "candidate"])
            self.assertNotIn(b"sensitive", raw)

    def test_challenge_empty_malformed_identity_counts_listings_and_conflicts_fail(self):
        for name in NAMES:
            site = next(s for s in contract.isolated_sources() if s.name == name)
            samples = ["", "<broken", "<h1>Shadow Slave</h1><p>9999 chapters 999999 views 4.9 ratings</p>",
                       '<title>Just a moment...</title><div id="challenge-form"></div>',
                       page(name, identity="Other Novel"), page(name).replace("<h1>Shadow Slave</h1>", ""),
                       page(name).replace("Chapter 3227:", "Chapter 3228:"),
                       page(name, href=site.url), page(name, href="https://evil.example/chapter-3227"),
                       page(name).replace("</section>", page(name, number=3226) + "</section>"),
                       page(name).replace("A New Dawn</a>", "A New Dawn</a><a href='https://evil.example/chapter-3227'>Chapter 3227: A New Dawn</a>"),
                       page(name).replace("Latest", "Total")]
            for html in samples:
                with self.subTest(name=name, html=html[:70]), \
                     patch.object(worker.http_client, "fetch_html", return_value=html):
                    self.assertEqual(worker.check_source(site, context())["outcome"], "parse_error")

    def test_source_above_target_is_rejected_without_candidate_or_authority_request(self):
        for site in contract.isolated_sources():
            with patch.object(worker.http_client, "fetch_html", return_value=page(site.name, number=3228)):
                result = worker.check_source(site, context())
            self.assertEqual(result["outcome"], "ahead_of_target")
            self.assertIsNone(result["chapter"])
            self.assertIsNone(result["url"])

    def test_same_number_with_different_chapter_urls_is_contradictory(self):
        site = contract.isolated_sources()[1]
        html = page(site.name, title=None).replace("</section>",
            '<a href="https://novelping.com/book/shadow-slave/chapter-3227-other-title">Chapter 3227</a></section>')
        with patch.object(worker.http_client, "fetch_html", return_value=html):
            self.assertEqual(worker.check_source(site, context())["outcome"], "parse_error")

    def test_suppression_and_deterministic_recovery_do_not_touch_peers(self):
        value = state()
        value["isolated_public_source_failures"] = {NAMES[0]: 4}
        with patch.object(worker, "recovery_window_open", return_value=False), \
             patch.object(worker.http_client, "fetch_html", side_effect=lambda site, **kw: page(site.name)) as fetch:
            data = json.loads(worker.produce(context(value)))
        self.assertEqual([e["outcome"] for e in data["sources"]], ["suppressed", "candidate"])
        self.assertEqual([call.args[0].name for call in fetch.call_args_list], [NAMES[1]])
        with patch.object(worker, "recovery_window_open", return_value=True), \
             patch.object(worker.http_client, "fetch_html", side_effect=lambda site, **kw: page(site.name)) as fetch:
            self.assertIsNotNone(worker.produce(context(value)))
        self.assertEqual(fetch.call_count, 2)
        for minute, expected in ((0, True), (4, True), (5, False), (29, False), (30, True), (35, False)):
            self.assertEqual(worker.recovery_window_open(NOW.replace(minute=minute)), expected)

    def test_no_target_first_run_and_authority_watch_produce_no_requests(self):
        for value in (state_manager.initial_state(), {**state(), "mode": "watch_webnovel"},
                      {**state(), "latest_seen": 3227}, {**state(), "latest_webnovel": 3226}):
            with patch.object(worker.http_client, "fetch_html") as fetch:
                self.assertIsNone(worker.produce(context(value)))
            fetch.assert_not_called()

    def test_cli_guards_actual_runner_event_and_parent_checkout_before_output(self):
        for version in ("24.04", "26.04"):
            with patch.object(worker.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": version}), \
                 patch.dict(os.environ, {"SHADOW_SLAVE_ISOLATED_RUNNER": "ubuntu-26.04", "GITHUB_EVENT_NAME": "workflow_run",
                                         "SHADOW_SLAVE_PARENT_SHA": "c" * 40}), \
                 patch.object(worker, "read_context", return_value=context()), \
                 patch.object(worker, "produce") as produce:
                self.assertEqual(worker.main(), 2)
            produce.assert_not_called()

    def test_cli_success_writes_only_the_bounded_artifact(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "evidence.json"
            with patch.object(worker.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "26.04"}), \
                 patch.dict(os.environ, {"SHADOW_SLAVE_ISOLATED_RUNNER": "ubuntu-26.04", "GITHUB_EVENT_NAME": "workflow_run",
                                         "SHADOW_SLAVE_PARENT_SHA": "a" * 40, "SHADOW_SLAVE_ISOLATED_OUTPUT": str(output)}), \
                 patch.object(worker, "read_context", return_value=context()), \
                 patch.object(worker.http_client, "fetch_html", side_effect=lambda site, **kw: page(site.name)), \
                 patch.object(state_manager, "save_state", side_effect=AssertionError("worker cannot save state")) as save, \
                 patch.object(state_manager, "atomic_write_json", side_effect=AssertionError("worker cannot persist")) as write:
                self.assertEqual(worker.main(), 0)
            self.assertEqual([path.name for path in Path(tmp).iterdir()], ["evidence.json"])
            self.assertEqual(len(contract.validate_evidence(output.read_bytes(), context(), state(), NOW)), 2)
            save.assert_not_called()
            write.assert_not_called()

    def test_actual_http_client_rejects_redirects_before_following_them(self):
        for site in contract.isolated_sources():
            for location in (site.url + "/", "https://evil.example/other-series"):
                response = Mock(status_code=302, is_redirect=True, url=site.url,
                                headers={"Location": location})
                session = Mock()
                session.get.return_value = response
                with patch.object(worker.http_client.requests, "Session", return_value=session):
                    result = worker.check_source(site, context())
                self.assertEqual(result["outcome"], "http_policy_error")
                session.get.assert_called_once()
                self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
                self.assertTrue(session.get.call_args.kwargs["stream"])

    def test_retry_after_cannot_delay_successful_peer_or_trigger_extra_attempts(self):
        for status in (429, 503):
            def get(url, **kwargs):
                response = Mock(status_code=status if "novelphoenix" in url else 200,
                                is_redirect=False, url=url, encoding="utf-8")
                response.headers = {"Content-Type": "text/html", "Retry-After": "120"}
                if response.status_code >= 400:
                    response.raise_for_status.side_effect = requests.HTTPError(response=response)
                response.iter_content.return_value = [page(NAMES[1]).encode()]
                return response
            with patch.object(worker.http_client.requests.Session, "get", side_effect=get) as request, \
                 patch.object(worker.http_client.time, "sleep", side_effect=AssertionError("worker cannot sleep for retries")) as sleep:
                raw = worker.produce(context())
            self.assertEqual(request.call_count, 2)
            self.assertEqual([e["outcome"] for e in json.loads(raw)["sources"]], ["http_error", "candidate"])
            sleep.assert_not_called()

    def test_narrowed_attempt_budget_keeps_timeout_bounded_and_rejects_invalid_limits(self):
        site = contract.isolated_sources()[0]
        with patch.object(worker.http_client.requests.Session, "get", side_effect=requests.Timeout()) as request, \
             patch.object(worker.http_client.time, "sleep", side_effect=AssertionError("worker cannot sleep for retries")):
            self.assertEqual(worker.check_source(site, context())["outcome"], "network_error")
        request.assert_called_once()
        for limit in (0, -1, 4, True, "1", 1.5):
            with patch.object(worker.http_client.requests.Session, "get") as request:
                with self.assertRaises(ValueError):
                    worker.http_client.fetch_html(site, max_attempts=limit)
            request.assert_not_called()

    def test_unresolved_non_200_metadata_cannot_be_reported_as_successful_parsing(self):
        site = contract.isolated_sources()[0]
        for status in (204, 301, 302, 304):
            document = HtmlDocument(page(site.name), ResponseMetadata(status, site.allowed_hosts[0], 1))
            with patch.object(worker.http_client, "fetch_html", return_value=document):
                self.assertEqual(worker.check_source(site, context())["outcome"], "http_policy_error")


class HandoffTransportTests(OfflineCase):
    def run_record(self, identifier=88):
        return {"id": identifier, "head_sha": "a" * 40, "event": "workflow_run", "head_branch": "main",
                "path": ".github/workflows/isolated-public-worker.yml"}

    def artifact_record(self):
        return {"id": 987, "name": handoff.artifact_name(context()), "expired": False, "size_in_bytes": 1000}

    def responses(self, artifacts=None, runs=None):
        return [encode({"workflow_runs": [self.run_record()] if runs is None else runs}),
                encode({"total_count": 1, "artifacts": [self.artifact_record()] if artifacts is None else artifacts}),
                "https://production.blob.core.windows.net/results/evidence.zip?sig=private",
                zipped(encode(payload()))]

    def test_download_is_scoped_to_parent_artifact_and_credentials_never_follow_redirect(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "private-test-token"}), \
             patch.object(handoff, "_get", side_effect=self.responses()) as get:
            self.assertEqual(handoff.download_current_evidence(context()), encode(payload()))
        calls = get.call_args_list
        self.assertIn("isolated-public-worker.yml/runs?event=workflow_run&head_sha=" + "a" * 40, calls[0].args[0])
        self.assertIn("/runs/88/artifacts?name=isolated-public-evidence-1234-2", calls[1].args[0])
        self.assertIn("/artifacts/987/zip", calls[2].args[0])
        self.assertIsNone(calls[3].args[1])
        self.assertTrue(all(call.args[1] == "private-test-token" for call in calls[:3]))

    def test_missing_not_finished_failed_skipped_worker_never_waits(self):
        for status, conclusion in (("queued", None), ("in_progress", None), ("completed", "failure"),
                                   ("completed", "skipped"), ("completed", "timed_out")):
            run = self.run_record() | {"status": status, "conclusion": conclusion}
            responses = [encode({"workflow_runs": [run]}), encode({"total_count": 0, "artifacts": []})]
            with self.subTest(status=status, conclusion=conclusion), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "test"}), \
                 patch.object(handoff, "_get", side_effect=responses) as get:
                self.assertIsNone(handoff.download_current_evidence(context()))
            self.assertEqual(get.call_count, 2)

    def test_duplicate_expired_oversized_and_misrouted_artifacts_rejected(self):
        for changes in ({"expired": True}, {"name": "isolated-public-evidence-1234-1"},
                        {"size_in_bytes": 32769}, {"size_in_bytes": True}, {"id": -1}):
            record = self.artifact_record() | changes
            with patch.dict(os.environ, {"GITHUB_TOKEN": "test"}), \
                 patch.object(handoff, "_get", side_effect=self.responses([record])):
                with self.assertRaises(contract.EvidenceError):
                    handoff.download_current_evidence(context())
        responses = self.responses()
        responses[1] = encode({"total_count": 2, "artifacts": [self.artifact_record()] * 2})
        with patch.dict(os.environ, {"GITHUB_TOKEN": "test"}), patch.object(handoff, "_get", side_effect=responses):
            with self.assertRaises(contract.EvidenceError):
                handoff.download_current_evidence(context())
        for changes in ({"head_sha": "b" * 40}, {"head_branch": "feature"}, {"event": "push"},
                        {"path": ".github/workflows/unrelated.yml"}, {"id": True}):
            with patch.dict(os.environ, {"GITHUB_TOKEN": "test"}), \
                 patch.object(handoff, "_get", side_effect=self.responses(runs=[self.run_record() | changes])):
                with self.assertRaises(contract.EvidenceError):
                    handoff.download_current_evidence(context())

    def test_unsafe_redirects_never_receive_credentials_or_a_request(self):
        for url in ("http://production.blob.core.windows.net/x", "https://evil.example/x",
                    "https://blob.core.windows.net.evil.example/x", "https://user@production.blob.core.windows.net/x",
                    "https://production.blob.core.windows.net:444/x", "https://production.blob.core.windows.net/x#fragment"):
            responses = self.responses()
            responses[2] = url
            with patch.dict(os.environ, {"GITHUB_TOKEN": "test"}), patch.object(handoff, "_get", side_effect=responses) as get:
                with self.assertRaises(contract.EvidenceError):
                    handoff.download_current_evidence(context())
            self.assertEqual(get.call_count, 3)

    def test_zip_member_and_decompression_bounds(self):
        for raw in (b"invalid zip", zipped(encode(payload()), "../evidence.json"),
                    zipped(encode(payload()), "other.json"), zipped(b"x" * 16385), b"x" * 32769):
            with self.subTest(size=len(raw)), self.assertRaises((contract.EvidenceError, zipfile.BadZipFile)):
                handoff.evidence_from_zip(raw)
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("evidence.json", "{}")
            archive.writestr("extra.json", "{}")
        with self.assertRaises(contract.EvidenceError):
            handoff.evidence_from_zip(raw.getvalue())

    def test_streamed_http_is_bounded_no_retry_and_deadline_checked(self):
        response = Mock(status_code=200)
        response.iter_content.return_value = [b"x" * 20]
        request = Mock()
        request.__enter__ = Mock(return_value=response)
        request.__exit__ = Mock(return_value=False)
        with patch.object(handoff.requests, "get", return_value=request) as get:
            with self.assertRaises(contract.EvidenceError):
                handoff._get("https://api.github.com/test", "test", handoff.time.monotonic() + 12, 10)
        self.assertEqual(get.call_count, 1)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertTrue(get.call_args.kwargs["stream"])
        with patch.object(handoff.requests, "get") as get:
            with self.assertRaises(contract.EvidenceError):
                handoff._get("https://api.github.com/test", "test", handoff.time.monotonic() - 1, 10)
        get.assert_not_called()


class MonitorIntegrationTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.ctx = context()
        self.enterContext(patch.object(handoff, "read_context", return_value=self.ctx))
        self.enterContext(patch.object(handoff, "utc_now", return_value=NOW))
        self.download = self.enterContext(patch.object(handoff, "download_current_evidence", return_value=encode(payload())))

    def local_report(self, site, *args):
        return ChapterReport(site.name, 3226, "Previous", site.url)

    def test_trusted_evidence_joins_existing_order_and_aggregates_once(self):
        value = state()
        with patch.object(monitor, "check_public_site", side_effect=self.local_report):
            reports = monitor.check_public_sites(RunResult(), value["public_source_failures"], {}, 3227,
                                                "A New Dawn", isolated_state=value)
        self.assertEqual([r.source for r in reports], [s.name for s in PUBLIC_SITES])
        self.assertEqual([r.source for r in reports if r.source not in NAMES], EXISTING)
        aggregate = monitor.aggregate_reports_for_chapter(reports, 3227)
        self.assertEqual(aggregate.source, ", ".join(NAMES))
        body = notification_body(3226, aggregate)
        for name in NAMES:
            site = next(s for s in PUBLIC_SITES if s.name == name)
            self.assertIn(f"{name} [{site.url}]", body)
            self.assertNotIn(chapter_url(name), body)

    def test_existing_and_remote_same_chapter_make_one_delivery_no_repeat(self):
        value = state()
        def local(site, *args):
            return ChapterReport(site.name, 3227, "A New Dawn", site.url)
        with patch.object(monitor, "check_public_site", side_effect=local), \
             patch.object(monitor, "send_new_chapter") as send, patch.object(monitor, "check_webnovel") as official:
            monitor.run_watch_free_sites(value, RunResult())
            self.assertEqual(send.call_count, 1)
            report = send.call_args.args[2]
            self.assertEqual(report.source, ", ".join(s.name for s in PUBLIC_SITES))
            self.assertEqual(value["latest_seen"], 3227)
            self.assertEqual(value["latest_webnovel"], 3227)
            self.assertEqual(handoff.collect(value, RunResult()), [])
        official.assert_not_called()

    def test_worker_failures_and_missing_evidence_preserve_existing_peers(self):
        for failure in (None, requests.Timeout(), contract.EvidenceError("provenance_mismatch"),
                        encode({"garbage": "malformed"})):
            value = state()
            self.download.side_effect = failure if isinstance(failure, Exception) else None
            self.download.return_value = None if isinstance(failure, Exception) else failure
            with patch.object(monitor, "check_public_site", side_effect=self.local_report):
                result = RunResult()
                reports = monitor.check_public_sites(result, value["public_source_failures"], {}, 3227,
                                                    isolated_state=value)
            self.assertEqual([r.source for r in reports], EXISTING)
            self.assertNotEqual(result.status, Health.FAILED)
            self.assertEqual(value["isolated_public_source_failures"], {})

    def test_integrity_rejection_cannot_notify_or_hide_local_failure(self):
        for data in (payload() | {"workflow_run_id": "other"}, payload() | {"base_sha256": "c" * 64}):
            self.download.return_value = encode(data)
            value = state()
            with patch.object(monitor, "check_public_site", side_effect=parsers.ParseError("chapter_data_missing")), \
                 patch.object(monitor, "send_new_chapter") as send:
                result = RunResult()
                monitor.run_watch_free_sites(value, result)
            self.assertEqual(result.status, Health.FAILED)
            self.assertEqual(value["latest_seen"], 3226)
            self.assertEqual(value["latest_webnovel"], 3227)
            send.assert_not_called()

    def test_remote_partial_success_survives_all_local_failures_and_suppression(self):
        data = payload()
        data["sources"][0] = entry(NAMES[0], "http_error")
        self.download.return_value = encode(data)
        for suppressed in (False, True):
            value = state()
            if suppressed:
                value["public_source_failures"] = dict.fromkeys(EXISTING, 4)
            with patch.object(monitor, "public_source_recovery_window_open", return_value=False), \
                 patch.object(monitor, "check_public_site", side_effect=parsers.ParseError("chapter_data_missing")), \
                 patch.object(monitor, "send_new_chapter") as send:
                result = RunResult()
                monitor.run_watch_free_sites(value, result)
            self.assertEqual(result.status, Health.DEGRADED)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[2].source, NAMES[1])

    def test_outage_counters_saturate_recover_and_never_reset_local_counters(self):
        value = state()
        value["public_source_failures"] = {"Chikari": 4}
        data = payload()
        data["sources"] = [entry(NAMES[0], "network_error"), entry(NAMES[1], "candidate")]
        self.download.return_value = encode(data)
        for _ in range(6):
            self.assertEqual([r.source for r in handoff.collect(value, RunResult())], [NAMES[1]])
        self.assertEqual(value["isolated_public_source_failures"], {NAMES[0]: 4})
        self.assertEqual(value["public_source_failures"], {"Chikari": 4})
        self.download.return_value = encode(payload())
        self.assertEqual(len(handoff.collect(value, RunResult())), 2)
        self.assertEqual(value["isolated_public_source_failures"], {})
        self.assertEqual(value["public_source_failures"], {"Chikari": 4})

    def test_pending_retry_range_and_backoff_are_preserved_without_duplicate_delivery(self):
        value = state()
        value["pending_notification"] = merge_pending(None, 3226, ChapterReport(NAMES[0], 3227, "A New Dawn", contract.isolated_sources()[0].url),
                                                       NotificationDeliveryError("http_error", 503))
        value["pending_notification"]["next_retry_at"] = "2099-01-01T00:00:00+00:00"
        before = copy.deepcopy(value["pending_notification"])
        with patch.object(monitor, "check_public_site", side_effect=self.local_report), \
             patch.object(monitor, "send_new_chapter") as send:
            result = RunResult()
            monitor.run_watch_free_sites(value, result)
        send.assert_not_called()
        pending = value["pending_notification"]
        for key in before.keys() - {"sources", "url"}:
            self.assertEqual(pending[key], before[key], key)
        self.assertEqual(pending["sources"], list(NAMES))
        self.assertEqual(result.status, Health.FAILED)
        self.assertEqual(value["latest_seen"], 3226)

    def test_due_pending_delivery_returns_before_worker_evidence(self):
        value = state()
        value["pending_notification"] = merge_pending(None, 3226, ChapterReport(NAMES[0], 3227, "A New Dawn", contract.isolated_sources()[0].url))
        value["pending_notification"]["next_retry_at"] = "2020-01-01T00:00:00+00:00"
        with patch.object(monitor, "send_new_chapter") as send:
            monitor.run_watch_free_sites(value, RunResult())
        self.assertEqual(send.call_count, 1)
        self.download.assert_not_called()
        self.assertEqual(value["mode"], "watch_webnovel")

    def test_authority_window_and_first_setup_never_lookup_worker(self):
        value = state()
        monitor.set_watch_webnovel(value)
        value["latest_webnovel"] = 3226
        with patch.object(monitor, "webnovel_due", return_value=True), \
             patch.object(monitor, "webnovel_check_window_open", return_value=True), \
             patch.object(monitor, "check_webnovel", return_value=ChapterReport("WebNovel", 3227, "A New Dawn", "https://www.webnovel.com/confirmed")) as official:
            monitor.run_watch_webnovel(value, RunResult())
        official.assert_called_once()
        self.download.assert_not_called()
        self.assertEqual(value["mode"], "watch_free_sites")
        first = state_manager.initial_state()
        with patch.object(monitor, "check_public_site", side_effect=self.local_report), \
             patch.object(monitor, "check_webnovel", return_value=ChapterReport("WebNovel", 3227, "A New Dawn", "https://www.webnovel.com/confirmed")), \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.first_setup(first, RunResult())
        self.download.assert_not_called()
        send.assert_not_called()
        self.assertEqual(first["latest_seen"], 3226)

    def test_existing_ahead_report_still_rechecks_authority_remote_cannot_advance_it(self):
        value = state()
        def local(site, *args):
            return ChapterReport(site.name, 3228, "Future", site.url)
        with patch.object(monitor, "check_public_site", side_effect=local), \
             patch.object(monitor, "check_webnovel", return_value=ChapterReport("WebNovel", 3227, "A New Dawn", "https://www.webnovel.com/confirmed")) as official, \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.run_watch_free_sites(value, RunResult())
        official.assert_called_once()
        self.assertEqual(send.call_args.args[2].chapter, 3227)
        self.assertEqual(value["latest_webnovel"], 3227)

    def test_repeated_unchanged_trusted_observations_do_not_save_state(self):
        value = state()
        data = payload()
        data["sources"] = [entry(name, number=3226) for name in NAMES]
        self.download.return_value = encode(data)
        original = copy.deepcopy(value)
        with patch.object(monitor, "load_state", return_value=(value, False)), \
             patch.object(monitor, "check_public_site", side_effect=self.local_report), \
             patch.object(monitor, "save_state") as save, patch.object(monitor, "write_result"), \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.main()
            monitor.main()
        save.assert_not_called()
        send.assert_not_called()
        self.assertEqual(value, original)


class StateAndWorkflowTests(OfflineCase):
    def test_additive_counter_validation_preserves_old_state_without_routine_write(self):
        old = state()
        old.pop("isolated_public_source_failures")
        old["public_source_failures"] = {"Chikari": 4}
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_bytes(encode(old))
            original = path.read_bytes()
            loaded, first = state_manager.load_state(path)
            self.assertFalse(first)
            self.assertEqual(loaded["isolated_public_source_failures"], {})
            self.assertEqual(loaded["public_source_failures"], {"Chikari": 4})
            with patch.object(state_manager, "atomic_write_json") as write:
                state_manager.save_state(loaded, path)
            write.assert_not_called()
            self.assertEqual(path.read_bytes(), original)
        for invalid in (None, [], {"WebNovel": 1}, {"Telegram": 1}, {NAMES[0]: True},
                        {NAMES[0]: 0}, {NAMES[1]: 5}, {NAMES[1]: "1"}):
            value = state()
            value["isolated_public_source_failures"] = invalid
            with self.assertRaises(state_manager.StateError):
                state_manager.validate_state(value)

    def test_stale_counter_changes_cannot_bypass_writer_lease_via_cursor_merge(self):
        spec = importlib.util.spec_from_file_location("isolated_state_writer_test", ROOT / "scripts/state_writer.py")
        writer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(writer)
        current = state()
        current["isolated_public_source_failures"] = {NAMES[0]: 4}
        artifact = copy.deepcopy(current)
        artifact["isolated_public_source_failures"] = {}
        artifact["source_positions"] = {"LightNovelUp": {"chapter": 3227,
            "url": "https://lightnovelup.com/novel/shadow-slave/chapter-3227-a-new-dawn/"}}
        self.assertFalse(writer.monitor_artifact_superseded(current, artifact))
        self.assertIsNone(writer.reconcile_monitor_cursor_advancement(current, artifact))

    def test_workflow_isolation_no_auxiliary_dependency_or_concurrency_slot(self):
        main = (ROOT / ".github/workflows/monitor.yml").read_text()
        auxiliary = (ROOT / ".github/workflows/isolated-public-worker.yml").read_text()
        main_job = main.split("  monitor:\n", 1)[1].split("  state-writer:\n", 1)[0]
        writer_job = main.split("  state-writer:\n", 1)[1]
        self.assertIn("runs-on: ubuntu-24.04", main_job)
        self.assertNotIn("needs:", main_job)
        self.assertNotIn("ubuntu-26.04", main)
        self.assertIn("needs: monitor", writer_job)
        self.assertIn("runs-on: ubuntu-latest", writer_job)
        self.assertIn("group: shadow-slave-monitor-workflow", main)
        self.assertIn("group: shadow-slave-state-writer", writer_job)
        self.assertIn("queue: max", main)
        self.assertIn("cancel-in-progress: false", main)
        self.assertIn("workflow_run:", auxiliary)
        self.assertIn("types: [in_progress]", auxiliary)
        self.assertIn("branches: [main]", auxiliary)
        self.assertIn("github.event.workflow_run.path == '.github/workflows/monitor.yml'", auxiliary)
        self.assertIn("runs-on: ubuntu-26.04", auxiliary)
        self.assertIn("timeout-minutes: 4", auxiliary)
        self.assertIn("contents: read", auxiliary)
        self.assertNotIn("concurrency:", auxiliary)
        self.assertNotIn("needs:", auxiliary)
        self.assertNotIn("secrets.", auxiliary)
        self.assertNotIn("contents: write", auxiliary)
        self.assertNotIn("actions: write", auxiliary)
        for forbidden in ("shadow_slave_monitor.monitor", "state_writer.py", "notifications", "diagnose_runner", "workflow_dispatch:"):
            self.assertNotIn(forbidden, auxiliary)
        commands = re.findall(r"run: (.+)", auxiliary)
        self.assertEqual(commands, ["python -m pip install --require-hashes -r dependencies/requirements.lock",
                                    "python -m shadow_slave_monitor.isolated_worker"])
        self.assertEqual([site.name for site in PUBLIC_SITES if site.enabled], EXISTING)
        self.assertEqual(PUBLIC_SITE_ORDER, {site.name: index for index, site in enumerate(PUBLIC_SITES)})

    def test_worker_lifecycle_cannot_change_watchdog_interpretation_of_parent_result(self):
        for worker_status in ("queued", "in_progress", "success", "failure", "skipped", "timed_out", "cancelled"):
            # Separate workflows have independent completion/concurrency. The
            # parent artifact, not any worker outcome, remains health authority.
            for result in ("healthy", "degraded", "failed"):
                run = {"id": 1234, "status": "completed", "conclusion": "success", "monitor_result": result}
                with self.subTest(worker_status=worker_status, monitor_result=result):
                    self.assertEqual(watchdog.monitor_completion_result(run), result)
        self.assertIsNone(watchdog.monitor_completion_result({"status": "completed", "conclusion": "failure", "monitor_result": None}))


if __name__ == "__main__":
    unittest.main()
