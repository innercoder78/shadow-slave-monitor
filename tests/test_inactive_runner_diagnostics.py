"""Offline coverage of optional inactive-reference runner observations."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests

from shadow_slave_monitor import diagnose_runner as diagnostic, parsers, state_manager
from shadow_slave_monitor.config import (
    CONNECT_TIMEOUT_SECONDS, HEADERS, HTTP_RETRIES, PUBLIC_SITES, READ_TIMEOUT_SECONDS,
)
from shadow_slave_monitor.models import ChapterReport


ROOT = Path(__file__).resolve().parents[1]
NAMES = ("Novel Phoenix", "NovelPing", "NovelFire")
SITES = tuple(next(site for site in PUBLIC_SITES if site.name == name) for name in NAMES)
PRIVATE = "private-secret https://untrusted.example/private?token=secret"
PHOENIX = ('<section><b>Latest Release:</b><a href="/novel/shadow-slave/chapter-3227">'
           'Chapter 3227 Private Title</a></section>')
FIRE = '<a href="/book/shadow-slave/chapter-3227">Chapter 3227 Private Title</a>'
PING = ('<h1>Shadow Slave</h1><section><b>Latest chapter:</b>'
        '<a href="/book/shadow-slave/chapter-3227-a-soul-and-a-shadow-walk-into-a-bar">'
        'Chapter 3227 A Soul and a Shadow Walk Into a Bar</a></section>')


def response(site, html=None, status=200):
    result = Mock(spec=requests.Response)
    result.status_code, result.url = status, site.url
    result.headers = {"Content-Type": "text/html"}
    result.encoding, result.is_redirect = "utf-8", False
    if html is None:
        html = {"Novel Phoenix": PHOENIX, "NovelFire": FIRE}.get(site.name, PRIVATE)
    result.iter_content.return_value = [html.encode()]
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(PRIVATE, response=result)
    return result


class InactiveRunnerDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.blocked = [self.enterContext(patch(target, side_effect=AssertionError("unexpected network")))
                        for target in ("requests.sessions.Session.request", "socket.socket.connect",
                                       "socket.socket.connect_ex", "socket.create_connection", "socket.getaddrinfo")]
        self.get = self.enterContext(patch("requests.Session.get", side_effect=self.respond))
        self.sleep = self.enterContext(patch("shadow_slave_monitor.http_client.time.sleep"))
        for target in ("save_state", "atomic_write_json", "load_state"):
            self.blocked.append(self.enterContext(patch.object(state_manager, target,
                                                               side_effect=AssertionError("state access"))))
        for target in ("send_new_chapter", "send_watchdog", "_post_ntfy"):
            self.blocked.append(self.enterContext(patch("shadow_slave_monitor.notifications." + target,
                                                       side_effect=AssertionError("notification"))))
        for target in ("check_public_site", "iter_public_candidates", "check_webnovel", "check_telegram_emergency"):
            self.blocked.append(self.enterContext(patch.object(parsers, target,
                                                               side_effect=AssertionError("production dispatch"))))
        self.blocked.append(self.enterContext(patch.object(diagnostic, "_baseline_snapshot",
                                                           side_effect=AssertionError("state snapshot"))))
        self.state_paths = (ROOT / "state/state.json", ROOT / "state/watchdog_state.json")
        self.state_before = [path.read_bytes() for path in self.state_paths]
        self.registry_before = tuple(PUBLIC_SITES)

    def tearDown(self):
        for blocked in self.blocked:
            blocked.assert_not_called()
        self.assertEqual([path.read_bytes() for path in self.state_paths], self.state_before)
        self.assertEqual(PUBLIC_SITES, self.registry_before)
        self.assertTrue(all(site.enabled is False for site in SITES))

    @staticmethod
    def respond(url, **kwargs):
        site = next((site for site in SITES if site.url == url), None)
        if site is None:
            raise AssertionError("unexpected URL")
        return response(site)

    def run_diagnostic(self):
        result = diagnostic.diagnose_inactive_sources()
        self.assertEqual([item["source"] for item in result["sources"]], list(NAMES))
        self.assert_safe(result)
        return result["sources"]

    def assert_safe(self, result):
        text = json.dumps(result)
        for forbidden in ("private-secret", "untrusted.example", "token=", "Private Title", "https://",
                          "<section", "<a ", "Traceback", "Authorization", "Cookie", "PRODUCTION_READY"):
            self.assertNotIn(forbidden, text)

    def test_exact_disabled_references_use_original_configuration_and_http_policy(self):
        with patch.object(diagnostic.http_client, "fetch_html", wraps=diagnostic.http_client.fetch_html) as fetch:
            results = self.run_diagnostic()
        self.assertEqual([item.args[0] for item in fetch.call_args_list], list(SITES))
        for item, site in zip(fetch.call_args_list, SITES):
            self.assertIs(item.args[0], site)
            self.assertEqual(item.kwargs, {})
        self.assertEqual([item.args[0] for item in self.get.call_args_list], [site.url for site in SITES])
        for item in self.get.call_args_list:
            self.assertEqual(item.kwargs, {"headers": HEADERS, "timeout": (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                                           "stream": True, "allow_redirects": False})
        self.assertTrue(all(item["http_completed"] and item["http_status"] == 200 for item in results))
        self.assertTrue(all(item["category"] == "HTTP_ACCESSIBLE" for item in results))

    def test_missing_enabled_or_duplicate_reference_fails_closed_before_any_request(self):
        for site in SITES:
            registries = (tuple(item for item in PUBLIC_SITES if item != site),
                          tuple(replace(item, enabled=True) if item == site else item for item in PUBLIC_SITES),
                          PUBLIC_SITES + (site,))
            for registry in registries:
                with self.subTest(name=site.name, registry=registry), patch.object(diagnostic, "PUBLIC_SITES", registry):
                    result = diagnostic.diagnose_inactive_sources()
                    self.assertEqual(result["setup_error"], "invalid_inactive_source_configuration")
                    self.assertEqual(result["sources"], [])
                    self.get.assert_not_called()

    def test_unexpected_identities_are_refused_without_echoing_input(self):
        for name in ("Telegram", "WebNovel", "NovelArrow", "novelfire", "NovelFire ", PRIVATE):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "^invalid_inactive_source_configuration$"):
                diagnostic.diagnose_inactive_source(name)
        self.get.assert_not_called()

    def test_valid_candidates_are_distinct_from_parser_unavailability(self):
        results = self.run_diagnostic()
        for result in (results[0], results[2]):
            self.assertTrue(result["parser_available"])
            self.assertTrue(result["parser_inspected"])
            self.assertEqual(result["parser_category"], "PARSER_CANDIDATE_FOUND")
            self.assertEqual(result["chapter_candidate"], 3227)
            self.assertFalse(result["additional_parser_work_required"])
            self.assertFalse(result["inconclusive"])
        rejected = results[1]
        self.assertTrue(rejected["parser_available"])
        self.assertTrue(rejected["parser_inspected"])
        self.assertEqual(rejected["parser_category"], "PARSER_NO_TRUSTWORTHY_CANDIDATE")
        self.assertTrue(rejected["additional_parser_work_required"])
        self.assertTrue(rejected["inconclusive"])
        # Keep genuine unavailability distinct from a dedicated parser's rejection.
        with patch.object(parsers, "parse_novelping_candidates", None):
            unavailable = self.run_diagnostic()[1]
        self.assertFalse(unavailable["parser_available"])
        self.assertFalse(unavailable["parser_inspected"])
        self.assertEqual(unavailable["parser_category"], "PARSER_UNAVAILABLE")
        self.assertTrue(unavailable["additional_parser_work_required"])
        self.assertTrue(unavailable["inconclusive"])

    def test_retained_parsers_are_called_directly_once_without_extra_requests(self):
        with patch.object(parsers, "parse_novel_phoenix_candidates", wraps=parsers.parse_novel_phoenix_candidates) as phoenix, \
             patch.object(parsers, "parse_novelping_candidates", wraps=parsers.parse_novelping_candidates) as ping, \
             patch.object(parsers, "parse_novelfire_candidates", wraps=parsers.parse_novelfire_candidates) as fire:
            self.run_diagnostic()
        phoenix.assert_called_once()
        ping.assert_called_once()
        fire.assert_called_once()
        self.assertEqual(self.get.call_count, 3)

    def test_all_three_dedicated_parsers_accept_independent_canonical_evidence(self):
        self.get.side_effect = lambda url, **kw: response(next(site for site in SITES if site.url == url),
                                                        {SITES[0].url: PHOENIX, SITES[1].url: PING, SITES[2].url: FIRE}[url])
        with patch.object(parsers, "parse_novelarrow_candidates", side_effect=AssertionError("legacy dispatch")) as legacy:
            results = self.run_diagnostic()
        for result in results:
            self.assertTrue(result["parser_available"] and result["parser_inspected"])
            self.assertEqual(result["parser_category"], "PARSER_CANDIDATE_FOUND")
            self.assertEqual(result["chapter_candidate"], 3227)
            self.assertEqual(result["jump_check"], "not_checked_without_reference")
            self.assertIsNone(result["reference_chapter"])
            self.assertIsNone(result["newer_than_reference"])
        legacy.assert_not_called()

    def test_explicit_reference_reports_older_unchanged_new_and_suspicious_results_without_state(self):
        self.get.side_effect = lambda url, **kw: response(next(site for site in SITES if site.url == url),
                                                        {SITES[0].url: PHOENIX, SITES[1].url: PING, SITES[2].url: FIRE}[url])
        for reference, newer in ((3226, True), (3227, False), (3228, False)):
            for result in diagnostic.diagnose_inactive_sources(reference_chapter=reference)["sources"]:
                self.assertEqual((result["chapter_candidate"], result["reference_chapter"], result["newer_than_reference"]),
                                 (3227, reference, newer))
                self.assertEqual(result["jump_check"], "within_limit")
        for result in diagnostic.diagnose_inactive_sources(reference_chapter=3201)["sources"]:
            self.assertTrue(result["http_completed"])
            self.assertIsNone(result["chapter_candidate"])
            self.assertEqual(result["parser_classification"], "suspicious_chapter_jump")
            self.assertEqual(result["jump_check"], "rejected")

    def test_invalid_references_refuse_all_requests_and_never_echo_input(self):
        for reference in (True, 0, 10001, "3227", PRIVATE):
            with self.subTest(reference=reference):
                self.assertIn("setup_error", diagnostic.diagnose_inactive_sources(reference_chapter=reference))
                self.get.assert_not_called()
        for reference in ("0", "10001", "-1", "3.5", PRIVATE, "3227\n"):
            output = io.StringIO()
            with patch.dict(diagnostic.os.environ, {"DIAGNOSTIC_MODE": "inactive_sources",
                                                   "DIAGNOSTIC_REFERENCE_CHAPTER": reference}), redirect_stdout(output):
                self.assertEqual(diagnostic.main(), 2)
            self.assert_safe(output.getvalue())
            self.get.assert_not_called()

    def test_structural_rejections_distinguish_listing_missing_section_and_bad_link(self):
        cases = ((SITES[2], '<a href="/book/shadow-slave/chapters">Chapter 3227 Private Title</a>',
                  "listing_without_individual_chapter"),
                 (SITES[0], PHOENIX.replace("Latest Release:", "Catalog:"), "latest_section_missing"),
                 (SITES[1], '<h1>Shadow Slave</h1>' + PING.split('</b>')[1].split('</section>')[0], "latest_section_missing"),
                 (SITES[0], PHOENIX.replace("chapter-3227", "chapter-3226"), "latest_link_invalid_or_contradictory"))
        for site, html, classification in cases:
            self.get.side_effect = lambda url, **kw: response(next(item for item in SITES if item.url == url), html)
            result = diagnostic.diagnose_inactive_source(site.name)
            self.assertEqual(result.parser_classification, classification)
            self.assertTrue(result.http_completed)
            self.assertIsNone(result.chapter_candidate)
            self.assertTrue(all(type(count) is int and 0 <= count <= 9999 for count in result.evidence.values()))

    def test_challenge_and_dynamic_shell_remain_unusable_without_fetching_scripts(self):
        for html, classification in (('<title>Just a moment...</title><form id="challenge-form"></form>' + PHOENIX,
                                      "challenge_page_suspected"),
                                     ('<h1>Shadow Slave</h1><script src="/chapters.js"></script><div id="app"></div>',
                                      "latest_section_missing")):
            self.get.side_effect = lambda url, **kw: response(SITES[0], html)
            result = diagnostic.diagnose_inactive_source(SITES[0].name)
            self.assertEqual(result.parser_classification, classification)
            self.assertIsNone(result.chapter_candidate)
        self.assertEqual(self.get.call_count, 2)

    def test_each_parser_failure_remains_independent_and_recovery_has_no_stale_result(self):
        for name, parser_name in zip(NAMES, ("parse_novel_phoenix_candidates", "parse_novelping_candidates", "parse_novelfire_candidates")):
            for error, category in ((parsers.ParseError("chapter_data_invalid"), "PARSER_NO_TRUSTWORTHY_CANDIDATE"),
                                    (RuntimeError(PRIVATE), "INTERNAL_ERROR")):
                with self.subTest(source=name, error=type(error)), patch.object(parsers, parser_name, side_effect=error):
                    results = self.run_diagnostic()
                    failed = next(item for item in results if item["source"] == name)
                    self.assertEqual(failed["parser_category"], category)
                    self.assertTrue(failed["http_completed"])
                    self.assertEqual(sum(item["http_completed"] for item in results), 3)
            recovered = next(item for item in self.run_diagnostic() if item["source"] == name)
            self.assertNotEqual(recovered["parser_category"], "INTERNAL_ERROR")
            if name != "NovelPing":
                self.assertEqual(recovered["chapter_candidate"], 3227)

    def test_access_failure_followed_by_recovery_reexamines_current_html(self):
        for site in SITES:
            self.get.side_effect = lambda url, **kw: response(site, status=403)
            self.assertFalse(diagnostic.diagnose_inactive_source(site.name).http_completed)
            self.get.side_effect = lambda url, **kw: response(site, PING if site.name == "NovelPing" else None)
            recovered = diagnostic.diagnose_inactive_source(site.name)
            self.assertEqual((recovered.http_status, recovered.chapter_candidate), (200, 3227))
            self.get.side_effect = lambda url, **kw: response(site, "<h1>Shadow Slave</h1>")
            self.assertIsNone(diagnostic.diagnose_inactive_source(site.name).chapter_candidate)

    def test_remaining_temporary_statuses_have_bounded_retries_for_every_source(self):
        for site in SITES:
            for status in (500, 502, 504):
                with self.subTest(source=site.name, status=status):
                    self.get.reset_mock()
                    self.get.side_effect = lambda url, **kw: response(site, status=status)
                    result = diagnostic.diagnose_inactive_source(site.name)
                    self.assertEqual((result.http_status, result.classification), (status, "server_error"))
                    self.assertEqual(self.get.call_count, HTTP_RETRIES)

    def test_parser_source_identity_is_rechecked_before_acceptance(self):
        candidate = ChapterReport("Other", 3227, "Private Title", SITES[0].url.replace("/chapters", "/chapter-3227"))
        with patch.object(parsers, "parse_novel_phoenix_candidates", return_value=[candidate]):
            result = self.run_diagnostic()[0]
        self.assertIsNone(result["chapter_candidate"])
        self.assertEqual(result["parser_classification"], "PARSE_CHAPTER_INVALID")

    def test_workflow_reference_is_optional_and_passed_as_environment_data(self):
        workflow = (ROOT / ".github/workflows/runner-compatibility-diagnostics.yml").read_text()
        self.assertIn('reference_chapter:\n', workflow)
        self.assertIn('DIAGNOSTIC_REFERENCE_CHAPTER: ${{ inputs.reference_chapter }}', workflow)
        self.assertNotIn('run: python -m shadow_slave_monitor.diagnose_runner ${{', workflow)

    def test_http_status_failures_and_bounded_retries_for_each_source(self):
        for site in SITES:
            for status, attempts, classification in ((403, 1, "access_denied"), (429, HTTP_RETRIES, "rate_limited"),
                                                       (503, HTTP_RETRIES, "server_error")):
                with self.subTest(source=site.name, status=status):
                    self.get.reset_mock()
                    self.get.side_effect = lambda url, **kw: response(site, status=status) if url == site.url else self.respond(url, **kw)
                    results = self.run_diagnostic()
                    result = next(item for item in results if item["source"] == site.name)
                    self.assertEqual((result["category"], result["http_status"], result["classification"]),
                                     ("HTTP_ERROR", status, classification))
                    self.assertFalse(result["http_completed"])
                    self.assertFalse(result["parser_inspected"])
                    self.assertEqual(sum(item.args[0] == site.url for item in self.get.call_args_list), attempts)
                    self.assertEqual(sum(item["http_completed"] for item in results), 2)

    def test_timeout_connection_tls_and_request_errors_are_sanitized(self):
        for error, category, classification, attempts in (
            (requests.ReadTimeout, "NETWORK_ERROR", "timeout", HTTP_RETRIES),
            (requests.ConnectTimeout, "NETWORK_ERROR", "timeout", HTTP_RETRIES),
            (requests.ConnectionError, "NETWORK_ERROR", "connection_error", HTTP_RETRIES),
            (requests.exceptions.SSLError, "TLS_ERROR", "tls_certificate_or_handshake_error", HTTP_RETRIES),
            (requests.RequestException, "NETWORK_ERROR", "request_error", 1),
        ):
            with self.subTest(error=error):
                self.get.reset_mock()
                self.get.side_effect = error(PRIVATE)
                results = self.run_diagnostic()
                self.assertEqual(self.get.call_count, attempts * 3)
                for item in results:
                    self.assertEqual((item["category"], item["classification"], item["http_status"]),
                                     (category, classification, "unavailable"))
                    self.assertFalse(item["http_completed"])

    def test_rejected_redirects_never_reach_unapproved_destinations(self):
        for location, reason in (("https://untrusted.example/private?token=secret", "redirect_host_not_allowed"),
                                 ("http://novelphoenix.com/private?token=secret", "non_https_url"),
                                 (None, "missing_redirect_location")):
            with self.subTest(location=location):
                self.get.reset_mock()
                bad = response(SITES[0], status=302)
                bad.is_redirect, bad.headers = True, {} if location is None else {"Location": location}
                self.get.side_effect = lambda url, **kw: bad if url == SITES[0].url else self.respond(url, **kw)
                result = self.run_diagnostic()[0]
                self.assertEqual((result["category"], result["classification"], result["http_status"]),
                                 ("HTTP_POLICY_ERROR", reason, 302))
                self.assertTrue(result["redirect_rejected"])
                self.assertFalse(result["http_completed"])
                self.assertFalse(result["parser_inspected"])
                self.assertEqual(self.get.call_count, 3)

    def test_allowed_redirect_uses_existing_validation_and_same_bounded_fetch(self):
        redirect = response(SITES[0], status=302)
        redirect.is_redirect = True
        redirect.headers = {"Location": "https://www.novelphoenix.com/novel/shadow-slave/chapters"}
        destination = response(SITES[0])
        destination.url = redirect.headers["Location"]
        self.get.side_effect = [redirect, destination, response(SITES[1]), response(SITES[2])]
        result = self.run_diagnostic()[0]
        self.assertTrue(result["http_completed"])
        self.assertFalse(result["redirect_rejected"])
        self.assertEqual(result["chapter_candidate"], 3227)
        self.assertEqual(self.get.call_count, 4)

    def test_real_response_without_redirect_location_is_not_http_accessible(self):
        bad = requests.Response()
        bad.status_code, bad.url, bad.encoding = 302, SITES[0].url, "utf-8"
        bad.headers = {"Content-Type": "text/html"}
        bad._content = PHOENIX.encode()
        bad._content_consumed = True
        self.assertFalse(bad.is_redirect)
        self.get.side_effect = lambda url, **kw: bad if url == SITES[0].url else self.respond(url, **kw)
        result = self.run_diagnostic()[0]
        self.assertEqual((result["category"], result["http_status"]), ("HTTP_POLICY_ERROR", 302))
        self.assertTrue(result["redirect_rejected"])
        self.assertFalse(result["http_completed"])
        self.assertFalse(result["parser_inspected"])

    def test_redirect_loop_is_bounded_and_classified_as_rejection(self):
        bad = response(SITES[0], status=302)
        bad.is_redirect, bad.headers = True, {"Location": SITES[0].url}
        self.get.side_effect = lambda url, **kw: bad if url == SITES[0].url else self.respond(url, **kw)
        result = self.run_diagnostic()[0]
        self.assertEqual(result["classification"], "redirect_limit")
        self.assertTrue(result["redirect_rejected"])
        self.assertEqual(self.get.call_count, 8)

    def test_content_type_and_response_size_rejection_keep_observed_200_incomplete(self):
        for oversized in (False, True):
            with self.subTest(oversized=oversized):
                bad = response(SITES[0])
                if not oversized:
                    bad.headers = {"Content-Type": "application/pdf"}
                self.get.side_effect = lambda url, **kw: bad if url == SITES[0].url else self.respond(url, **kw)
                with patch.object(diagnostic.http_client, "MAX_HTML_BYTES", 1 if oversized else 5 * 1024 * 1024):
                    result = self.run_diagnostic()[0]
                self.assertEqual(result["category"], "HTTP_POLICY_ERROR")
                self.assertEqual(result["classification"], "response_too_large" if oversized else "unexpected_content_type")
                self.assertEqual(result["http_status"], 200)
                self.assertFalse(result["http_completed"])
                self.assertFalse(result["redirect_rejected"])

    def test_streaming_failure_does_not_count_received_200_as_completed(self):
        bad = response(SITES[0])
        bad.iter_content.side_effect = requests.ConnectionError(PRIVATE)
        self.get.side_effect = lambda url, **kw: bad if url == SITES[0].url else self.respond(url, **kw)
        result = self.run_diagnostic()[0]
        self.assertEqual((result["category"], result["http_status"], result["http_completed"]),
                         ("NETWORK_ERROR", 200, False))

    def test_missing_misleading_and_malformed_evidence_never_invents_candidates(self):
        for html in ("", "<html><p>No releases</p></html>", "<a href='><b>Latest Release",
                     "<p>9999 Chapters; 4.9 rating; 2026-10-27; 123456 views</p><nav>1 2 3 Next</nav>",
                     '<a href="/book/shadow-slave/chapters">Chapter 3227 Private Title</a>'):
            with self.subTest(html=html):
                self.get.side_effect = lambda url, **kw: response(next(site for site in SITES if site.url == url), html)
                for result in self.run_diagnostic():
                    self.assertEqual(result["category"], "HTTP_ACCESSIBLE")
                    self.assertIsNone(result["chapter_candidate"])
                    self.assertTrue(result["inconclusive"])
                    self.assertTrue(result["additional_parser_work_required"])
                    self.assertEqual(result["parser_category"], "PARSER_NO_TRUSTWORTHY_CANDIDATE"
                                     if result["parser_available"] else "PARSER_UNAVAILABLE")

    def test_invalid_chapter_ranges_and_noncanonical_urls_are_rejected(self):
        for number in (0, 10001):
            self.get.side_effect = lambda url, **kw: response(next(site for site in SITES if site.url == url),
                                                             (PHOENIX + FIRE).replace("3227", str(number)))
            self.assertTrue(all(item["chapter_candidate"] is None for item in self.run_diagnostic()))
        for href in ("https://untrusted.example/book/shadow-slave/chapter-3227",
                     "https://user:password@novelfire.net/book/shadow-slave/chapter-3227",
                     "https://novelfire.net:443/book/shadow-slave/chapter-3227",
                     "/book/shadow-slave/chapter-3227?token=secret", "/book/other/chapter-3227",
                     "/book/shadow-slave/chapter-3226"):
            self.get.side_effect = lambda url, **kw: response(next(site for site in SITES if site.url == url),
                                                             f'<a href="{href}">Chapter 3227 Private Title</a>')
            self.assertIsNone(self.run_diagnostic()[2]["chapter_candidate"])

    def test_shared_chapter_and_url_validation_rechecks_retained_parser_results(self):
        for candidate in (ChapterReport("", True, "Private Title", SITES[0].url),
                          ChapterReport("", 10001, "Private Title", SITES[0].url),
                          ChapterReport("", 3227, "Private Title", PRIVATE)):
            with self.subTest(candidate=candidate), patch.object(parsers, "parse_novel_phoenix_candidates", return_value=[candidate]):
                self.assertIsNone(self.run_diagnostic()[0]["chapter_candidate"])

    def test_parser_errors_remain_separate_from_successful_http(self):
        for error, category in ((parsers.ParseError("chapter_data_invalid"), "PARSER_NO_TRUSTWORTHY_CANDIDATE"),
                                (RuntimeError(PRIVATE), "INTERNAL_ERROR")):
            with self.subTest(error=type(error)), patch.object(parsers, "parse_novel_phoenix_candidates", side_effect=error):
                results = self.run_diagnostic()
                result = results[0]
                self.assertEqual((result["category"], result["http_status"], result["http_completed"]),
                                 ("HTTP_ACCESSIBLE", 200, True))
                self.assertEqual(result["parser_category"], category)
                self.assertEqual(results[2]["chapter_candidate"], 3227)

    def test_internal_transport_error_does_not_suppress_other_sources(self):
        def fail_one(url, **kwargs):
            if url == SITES[0].url:
                raise RuntimeError(PRIVATE)
            return self.respond(url, **kwargs)
        self.get.side_effect = fail_one
        results = self.run_diagnostic()
        self.assertEqual(results[0]["category"], "INTERNAL_ERROR")
        self.assertEqual(results[2]["chapter_candidate"], 3227)
        self.assertEqual(self.get.call_count, 3)

    def test_source_logs_are_suppressed_and_logging_is_restored(self):
        level = logging.root.manager.disable
        def noisy(url, **kwargs):
            logging.critical(PRIVATE)
            return self.respond(url, **kwargs)
        self.get.side_effect = noisy
        output = io.StringIO()
        with redirect_stderr(output):
            self.run_diagnostic()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(logging.root.manager.disable, level)

    def test_runner_metadata_compares_identical_sources_on_both_labels(self):
        outcomes = []
        for label in ("ubuntu-24.04", "ubuntu-26.04"):
            with patch.dict(diagnostic.os.environ, {"DIAGNOSTIC_RUNNER_LABEL": label,
                                                   "DIAGNOSTIC_CHECKOUT_SHA": "a" * 40}), \
                 patch("platform.freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": label[7:]}):
                result = diagnostic.diagnose_inactive_sources()
            self.assertEqual(result["runner"]["requested_runner"], label)
            self.assertEqual(result["runner"]["os_version"], label[7:])
            self.assertEqual(result["runner"]["checkout_sha"], "a" * 40)
            outcomes.append(result)
        self.assertEqual(outcomes[0]["sources"], outcomes[1]["sources"])

    def test_inactive_main_output_is_sanitized_and_inconclusive_results_warn(self):
        with TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            output = io.StringIO()
            with patch.dict(diagnostic.os.environ, {"DIAGNOSTIC_MODE": "inactive_sources", "GITHUB_STEP_SUMMARY": str(summary)}), \
                 redirect_stdout(output), redirect_stderr(output):
                self.assertEqual(diagnostic.main(), 0)
            for text in (output.getvalue(), summary.read_text(encoding="utf-8")):
                self.assert_safe(text)
                self.assertIn("all sources remain disabled", text)
                self.assertIn("INCONCLUSIVE OR FAILED CHECKS", text)
                self.assertIn("PARSER_NO_TRUSTWORTHY_CANDIDATE", text)
            self.assertIn("::warning::", output.getvalue())

    def test_setup_or_internal_parser_failures_exit_nonzero_without_traceback(self):
        for setup in (True, False):
            output = io.StringIO()
            with patch.dict(diagnostic.os.environ, {"DIAGNOSTIC_MODE": "inactive_sources", "GITHUB_STEP_SUMMARY": ""}), \
                 patch.object(diagnostic, "PUBLIC_SITES", () if setup else PUBLIC_SITES), \
                 patch.object(parsers, "parse_novel_phoenix_candidates", side_effect=RuntimeError(PRIVATE)), \
                 redirect_stdout(output), redirect_stderr(output):
                self.assertEqual(diagnostic.main(), 2)
            self.assert_safe(output.getvalue())

    def test_invalid_mode_makes_no_request_and_does_not_echo_input(self):
        output = io.StringIO()
        with patch.dict(diagnostic.os.environ, {"DIAGNOSTIC_MODE": PRIVATE}), redirect_stdout(output):
            self.assertEqual(diagnostic.main(), 2)
        self.get.assert_not_called()
        self.assert_safe(output.getvalue())


class DiagnosticModeTests(unittest.TestCase):
    def test_core_ignores_inactive_reference_input_and_keeps_its_result_shape(self):
        result = {"webnovel": {"chapter_validated": True, "category": "SUCCESS_VALID_CHAPTER"},
                  "telegram_emergency": {"chapter_validated": True, "category": "SUCCESS_VALID_CHAPTER"}}
        output = io.StringIO()
        with patch.dict(diagnostic.os.environ, {"DIAGNOSTIC_MODE": "core", "DIAGNOSTIC_REFERENCE_CHAPTER": PRIVATE,
                                               "GITHUB_STEP_SUMMARY": ""}), \
             patch.object(diagnostic, "diagnose_runner", return_value=result) as core, \
             patch.object(diagnostic, "diagnose_inactive_sources") as inactive, redirect_stdout(output):
            self.assertEqual(diagnostic.main(), 0)
        core.assert_called_once_with()
        inactive.assert_not_called()
        self.assertNotIn("reference_chapter", output.getvalue())
        self.assertNotIn(PRIVATE, output.getvalue())

    def test_core_remains_the_default_and_does_not_resolve_inactive_sources(self):
        core_result = {"webnovel": {"chapter_validated": True, "category": "SUCCESS_VALID_CHAPTER"},
                       "telegram_emergency": {"chapter_validated": True, "category": "SUCCESS_VALID_CHAPTER"}}
        with patch.dict(diagnostic.os.environ, {}, clear=True), \
             patch.object(diagnostic, "diagnose_runner", return_value=core_result) as core, \
             patch.object(diagnostic, "diagnose_inactive_sources") as inactive, redirect_stdout(io.StringIO()):
            self.assertEqual(diagnostic.main(), 0)
        core.assert_called_once_with()
        inactive.assert_not_called()

    def test_workflow_choice_is_env_mapped_and_preserves_existing_matrix(self):
        workflow = (ROOT / ".github/workflows/runner-compatibility-diagnostics.yml").read_text(encoding="utf-8")
        self.assertIn("diagnostic_mode:\n", workflow)
        self.assertIn("type: choice\n", workflow)
        self.assertIn("default: core\n", workflow)
        self.assertIn("options:\n          - core\n          - inactive_sources\n", workflow)
        self.assertIn("DIAGNOSTIC_MODE: ${{ inputs.diagnostic_mode }}", workflow)
        self.assertIn("runner: [ubuntu-24.04, ubuntu-26.04]", workflow)
        self.assertIn("ref: ${{ github.sha }}", workflow)
        self.assertIn("fail-fast: false", workflow)
        self.assertEqual(workflow.count("${{ inputs.diagnostic_mode }}"), 1)
        self.assertEqual(workflow.count("run: python -m shadow_slave_monitor.diagnose_runner"), 1)


if __name__ == "__main__":
    unittest.main()
