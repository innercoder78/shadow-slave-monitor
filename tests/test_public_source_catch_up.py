"""Synthetic terminal-navigation regressions; all I/O is mocked."""
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import call, patch

import requests

from shadow_slave_monitor import monitor, parsers
from shadow_slave_monitor.config import SourceConfig
from shadow_slave_monitor.http_client import HttpFetchError
from shadow_slave_monitor.models import ChapterReport, Health, RunResult
import test_public_source_verification as fixtures


SOURCE = fixtures.NAVIGATION
CURSOR = {"chapter": 3193, "url": SOURCE.url + "chapter-3193-regret-can/"}


def listing(*numbers):
    return {"items": [fixtures.nav_record(n, f"Release {n}", f"chapter-{n}-release-{n}")
                      for n in numbers]}


def page(number):
    return fixtures.nav_page(number, f"Release {number}", f"chapter-{number}-release-{number}")


class TerminalCatchUpTests(unittest.TestCase):
    def check(self, data, *, target=3195, selected=3194, identity=None, document=None):
        original = dict(CURSOR)
        responses = [json.dumps(fixtures.NAV_IDENTITY if identity is None else identity),
                     json.dumps(data)]
        with patch.object(parsers, "fetch_json", side_effect=responses) as api, \
             patch.object(parsers, "fetch_html", side_effect=[
                 fixtures.nav_page(), page(selected) if document is None else document]) as html:
            report = parsers.check_public_site(SOURCE, CURSOR, target)
        self.assertEqual(CURSOR, original)
        self.assertEqual(api.call_args_list, [call(SOURCE, fixtures.NAV_API),
                                             call(SOURCE, fixtures.NAV_API + "/chapters")])
        return report, html.call_args_list

    def assert_retained(self, report, calls):
        self.assertEqual((report.chapter, report.position_chapter, report.url, report.position_url),
                         (3193, 3193, CURSOR["url"], CURSOR["url"]))
        self.assertEqual(report.strategy, "LightNovelUp:canonical_navigation")
        self.assertEqual(calls, [call(SOURCE, CURSOR["url"])])

    def test_newer_numbered_record_is_confirmed_without_next_link(self):
        report, calls = self.check(listing(3194, 3193))
        self.assertEqual((report.chapter, report.position_chapter), (3194, 3194))
        self.assertEqual(report.strategy, "LightNovelUp:verified_api_record")
        self.assertEqual(calls, [call(SOURCE, CURSOR["url"]),
                                call(SOURCE, report.url, allow_redirects=False)])

    def test_authoritative_target_is_preferred_over_highest_record(self):
        report, calls = self.check(listing(3196, 3194, 3195), selected=3195)
        self.assertEqual(report.chapter, 3195)
        self.assertEqual(calls[-1], call(SOURCE, report.url, allow_redirects=False))

    def test_missing_target_uses_highest_verified_number_without_inference(self):
        data = listing(3193, 3194)
        data["items"][0]["chapter_index"] = "9999"
        report, calls = self.check(data, target=3210)
        self.assertEqual(report.chapter, 3194)
        self.assertEqual(len(calls), 2)

    def test_same_or_older_records_preserve_cursor_without_page_request(self):
        for numbers in [(3193,), (3192,), (3192, 3193)]:
            with self.subTest(numbers=numbers):
                self.assert_retained(*self.check(listing(*numbers)))

    def test_no_api_traffic_when_target_does_not_require_catch_up(self):
        for target in [None, 3193, 3192, True, "3195", 0, 10001]:
            with self.subTest(target=target), \
                 patch.object(parsers, "fetch_html", return_value=fixtures.nav_page()) as html, \
                 patch.object(parsers, "fetch_json") as api:
                report = parsers.check_public_site(SOURCE, CURSOR, target)
            api.assert_not_called()
            self.assert_retained(report, html.call_args_list)

    def test_invalid_listing_and_duplicate_records_preserve_verified_position(self):
        cases = [None, {}, [], {"items": []}, {"items": {}}, {"items": [None]},
                 {"items": listing(3194)["items"] * 51}, listing(3194, 3194),
                 {"items": [dict(fixtures.nav_record(), post_id="other")]},
                 {"items": [dict(fixtures.nav_record(), chapter_name=None)]},
                 {"items": [dict(fixtures.nav_record(), chapter_name="Chapter 3194 Conflict")]},
                 {"items": [dict(fixtures.nav_record(), chapter_slug="chapter-regret-can",
                                 chapter_name="Chapter Regret Can", chapter_index="3195")]},
                 # Stale records still undergo ambiguity validation before filtering.
                 listing(3193, 3193)]
        for data in cases:
            with self.subTest(data=data), self.assertLogs(level="WARNING"):
                self.assert_retained(*self.check(data))

    def test_wrong_series_identity_stops_before_listing_request(self):
        for field, value in [("id", "1883008"), ("title", "Other Series"),
                             ("permalink", SOURCE.url + "?token=secret")]:
            identity = copy.deepcopy(fixtures.NAV_IDENTITY)
            identity["novel"][field] = value
            with self.subTest(field=field), self.assertLogs(level="WARNING"), \
                 patch.object(parsers, "fetch_html", return_value=fixtures.nav_page()) as html, \
                 patch.object(parsers, "fetch_json", return_value=json.dumps(identity)) as api:
                report = parsers.check_public_site(SOURCE, CURSOR, 3195)
            api.assert_called_once_with(SOURCE, fixtures.NAV_API)
            self.assert_retained(report, html.call_args_list)

    def test_malformed_json_and_optional_transport_failures_are_sanitized(self):
        for failure in ["{private token=secret", '{"novel":{},"novel":{}}',
                        requests.Timeout("token=secret"), requests.ConnectionError("token=secret"),
                        HttpFetchError("redirect_not_allowed"), RuntimeError("token=secret")]:
            with self.subTest(failure=type(failure).__name__), self.assertLogs(level="WARNING") as logs, \
                 patch.object(parsers, "fetch_html", return_value=fixtures.nav_page()) as html, \
                 patch.object(parsers, "fetch_json", side_effect=[failure]) as api:
                report = parsers.check_public_site(SOURCE, CURSOR, 3195)
            self.assert_retained(report, html.call_args_list)
            api.assert_called_once()
            self.assertNotIn("secret", "\n".join(logs.output))
            self.assertNotIn("private", "\n".join(logs.output))

    def test_unsafe_urls_and_title_only_records_are_never_fetched(self):
        for slug in ["chapter-3194-release-3194?token=secret", "chapter-3194-release-3194#secret",
                     "chapter-3194-%72elease", "../other/chapter-3194-release-3194",
                     "https://evil.example/chapter-3194-release-3194", "chapter-release"]:
            data = listing(3194)
            data["items"][0]["chapter_slug"] = slug
            with self.subTest(slug=slug), self.assertLogs(level="WARNING"):
                self.assert_retained(*self.check(data))

    def test_selected_page_requires_all_existing_confirmation_evidence(self):
        good = page(3194)
        cases = ["<html></html>", good.replace("Shadow Slave", "Other Series"),
                 good.replace("Chapter 3194", "Chapter 3192"),
                 good.replace('id="chapter-heading"', 'id="other"'),
                 good.replace('rel="canonical"', 'rel="other"'),
                 good.replace('release-3194/"', 'release-3194/?token=secret"'),
                 good.replace("Synthetic chapter text.", " "),
                 good + '<h2>Chapter 3194 Contradictory</h2>',
                 good + '<title>Chapter 3192 Contradictory</title>',
                 good + '<title>Just a moment...</title><form id="challenge-form"></form>']
        for document in cases:
            with self.subTest(document=cases.index(document)), self.assertLogs(level="WARNING"):
                report, calls = self.check(listing(3194), document=document)
            self.assertEqual(report.position_chapter, 3193)
            self.assertEqual(calls[-1], call(SOURCE, SOURCE.url + "chapter-3194-release-3194/",
                                           allow_redirects=False))

    def test_selected_page_transport_failure_retains_cursor(self):
        for failure in [requests.Timeout("token=secret"), HttpFetchError("redirect_not_allowed")]:
            with self.subTest(failure=type(failure).__name__), self.assertLogs(level="WARNING"), \
                 patch.object(parsers, "fetch_html", side_effect=[fixtures.nav_page(), failure]) as html, \
                 patch.object(parsers, "fetch_json", side_effect=[json.dumps(fixtures.NAV_IDENTITY),
                                                                json.dumps(listing(3194))]):
                report = parsers.check_public_site(SOURCE, CURSOR, 3195)
            self.assertEqual(report.position_chapter, 3193)
            self.assertFalse(html.call_args.kwargs["allow_redirects"])

    def test_unverified_current_page_or_unsafe_navigation_cannot_use_recovery(self):
        for document in ['<h1>Another Series - Chapter 3193 Regret Can</h1>',
                         fixtures.nav_page() + '<h2>Chapter 3192 Conflict</h2>',
                         fixtures.nav_page() + '<a href="https://evil.example/secret">Next</a>',
                         fixtures.nav_page() + '<a href="chapter-3194-first/">Next</a>'
                         '<a href="chapter-3194-second/">Next</a>',
                         fixtures.nav_page() + '<a href="chapter-3192-previous/">Next</a>']:
            with self.subTest(document=document), \
                 patch.object(parsers, "fetch_html", return_value=document), \
                 patch.object(parsers, "fetch_json") as api, self.assertRaises(parsers.ParseError):
                parsers.check_public_site(SOURCE, CURSOR, 3195)
            api.assert_not_called()

    def test_numbered_navigation_reaches_target_without_api_traffic(self):
        next_url = SOURCE.url + "chapter-3194-release-3194/"
        with patch.object(parsers, "fetch_html", side_effect=[
                fixtures.nav_page() + f'<a href="{next_url}">Next</a>', page(3194)]) as html, \
             patch.object(parsers, "fetch_json") as api:
            report = parsers.check_public_site(SOURCE, CURSOR, 3194)
        self.assertEqual(report.chapter, 3194)
        self.assertEqual(html.call_count, 2)
        api.assert_not_called()


class TerminalCatchUpIntegrationTests(unittest.TestCase):
    def state(self, target=3210):
        state = fixtures.waiting_state()
        state.update(latest_seen=target - 1, latest_webnovel=target, target_chapter=target,
                     target_title=f"Release {target}")
        state["source_positions"] = {SOURCE.name: dict(CURSOR)}
        return state

    def test_verified_intermediate_catch_up_advances_only_source_cursor(self):
        state = self.state()
        with patch.object(monitor, "PUBLIC_SITES", (SOURCE,)), \
             patch.object(parsers, "fetch_html", side_effect=[fixtures.nav_page(), page(3194)]), \
             patch.object(parsers, "fetch_json", side_effect=[json.dumps(fixtures.NAV_IDENTITY),
                                                            json.dumps(listing(3194))]), \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.run_watch_free_sites(state, RunResult())
        self.assertEqual(state["source_positions"][SOURCE.name]["chapter"], 3194)
        self.assertEqual((state["latest_seen"], state["target_chapter"]), (3209, 3210))
        send.assert_not_called()

    def test_cursor_is_persisted_only_after_independent_page_verification(self):
        state = self.state()
        for valid in [False, True]:
            def html(site, url=None, **kwargs):
                # The controlling monitor has not advanced while verification runs.
                self.assertEqual(state["source_positions"][SOURCE.name], CURSOR)
                if url == CURSOR["url"]:
                    return fixtures.nav_page()
                return page(3194) if valid else "<html></html>"
            with self.subTest(valid=valid), \
                 patch.object(monitor, "PUBLIC_SITES", (SOURCE,)), \
                 patch.object(monitor, "load_state", return_value=(state, False)), \
                 patch.object(parsers, "fetch_html", side_effect=html), \
                 patch.object(parsers, "fetch_json", side_effect=[json.dumps(fixtures.NAV_IDENTITY),
                                                                json.dumps(listing(3194))]), \
                 patch.object(monitor, "save_state") as save, \
                 patch.object(monitor, "write_result"), \
                 patch.object(monitor, "send_new_chapter") as send:
                monitor.main()
            self.assertEqual(save.call_count, int(valid))
            self.assertEqual(state["source_positions"][SOURCE.name]["chapter"], 3194 if valid else 3193)
            self.assertEqual(state["latest_seen"], 3209)
            send.assert_not_called()

    def test_verified_target_can_notify_and_advance_cursor(self):
        state = self.state(3195)
        with patch.object(monitor, "PUBLIC_SITES", (SOURCE,)), \
             patch.object(parsers, "fetch_html", side_effect=[fixtures.nav_page(), page(3195)]), \
             patch.object(parsers, "fetch_json", side_effect=[json.dumps(fixtures.NAV_IDENTITY),
                                                            json.dumps(listing(3196, 3195))]), \
             patch.object(monitor, "check_webnovel") as official, \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.run_watch_free_sites(state, RunResult())
        official.assert_not_called()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[2].chapter, 3195)
        self.assertEqual(state["latest_seen"], 3195)
        self.assertEqual(state["source_positions"][SOURCE.name]["chapter"], 3195)

    def test_above_target_record_still_requires_authoritative_recheck(self):
        state = self.state(3195)
        with patch.object(monitor, "PUBLIC_SITES", (SOURCE,)), \
             patch.object(parsers, "fetch_html", side_effect=[fixtures.nav_page(), page(3196)]), \
             patch.object(parsers, "fetch_json", side_effect=[json.dumps(fixtures.NAV_IDENTITY),
                                                            json.dumps(listing(3196))]), \
             patch.object(monitor, "check_webnovel", return_value=ChapterReport(
                 "WebNovel", 3195, "Release 3195", "https://www.webnovel.com/book/catalog")) as official, \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.run_watch_free_sites(state, RunResult())
        official.assert_called_once()
        send.assert_not_called()
        self.assertEqual(state["latest_seen"], 3194)

    def test_stale_missing_or_unavailable_api_does_not_write_state_or_notify(self):
        for responses in [[json.dumps(fixtures.NAV_IDENTITY), json.dumps(listing(3192))],
                          [json.dumps(fixtures.NAV_IDENTITY), json.dumps(listing(3193))],
                          [json.dumps(fixtures.NAV_IDENTITY), json.dumps({"items": []})],
                          [json.dumps(fixtures.NAV_IDENTITY), requests.Timeout("token=secret")],
                          [requests.Timeout("token=secret")]]:
            state = self.state()
            original = copy.deepcopy(state)
            with self.subTest(responses=responses), \
                 patch.object(monitor, "PUBLIC_SITES", (SOURCE,)), \
                 patch.object(monitor, "load_state", return_value=(state, False)), \
                 patch.object(parsers, "fetch_html", return_value=fixtures.nav_page()) as html, \
                 patch.object(parsers, "fetch_json", side_effect=responses), \
                 patch.object(monitor, "save_state") as save, \
                 patch.object(monitor, "write_result") as result, \
                 patch.object(monitor, "send_new_chapter") as send:
                monitor.main()
            self.assertEqual(state, original)
            self.assertEqual(result.call_args.args[0].status, Health.HEALTHY)
            html.assert_called_once_with(SOURCE, CURSOR["url"])
            save.assert_not_called()
            send.assert_not_called()

    def test_optional_failure_isolated_and_suppressed_source_recovers(self):
        other = SourceConfig("Other", "https://other.example", True, ("other.example",))
        original_check = monitor.check_public_site
        def check(site, *args):
            if site == other:
                raise parsers.ParseError("chapter_data_missing")
            return original_check(site, *args)
        failures = {SOURCE.name: 4}
        positions = {SOURCE.name: dict(CURSOR)}
        result = RunResult()
        with patch.object(monitor, "PUBLIC_SITES", (SOURCE, other)), \
             patch.object(monitor, "public_source_recovery_window_open", return_value=True), \
             patch.object(monitor, "check_public_site", side_effect=check), \
             patch.object(parsers, "fetch_html", return_value=fixtures.nav_page()), \
             patch.object(parsers, "fetch_json", side_effect=requests.Timeout("token=secret")):
            reports = monitor.check_public_sites(result, failures, positions, 3195)
        self.assertEqual([r.chapter for r in reports], [3193])
        self.assertEqual(failures, {other.name: 1})
        self.assertEqual(positions, {SOURCE.name: CURSOR})
        self.assertEqual(result.status, Health.DEGRADED)
