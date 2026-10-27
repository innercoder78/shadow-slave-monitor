"""Target-only source tests. All chapter prose below is synthetic.

The wrapper, canonical link and titled numbered h2 reflect the two live examples
inspected on 2026-10-25. Those pages have NO novel label. The placeholder has a
bare h1 between adjacent chapter links. All body prose below is invented.
No fixture is a capture of a real chapter.
"""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests
from bs4 import BeautifulSoup

from shadow_slave_monitor import diagnose_source, monitor, parsers
from shadow_slave_monitor.config import PUBLIC_SITES, PUBLIC_SITE_ORDER
from shadow_slave_monitor.http_client import HttpFetchError, fetch_html
from shadow_slave_monitor.models import ChapterReport, Health, RunResult
from shadow_slave_monitor.notifications import NotificationDeliveryError, notification_body
from shadow_slave_monitor.state_manager import initial_state, validate_source_config, validate_state


CONFIG = PUBLIC_SITES[-1]
ENABLED = replace(CONFIG, enabled=True)
OTHER = PUBLIC_SITES[0]
PARAGRAPHS = (
    "The cartographer opened a wooden box beside the workshop window. Inside lay a brass compass, "
    "several folded maps, and a small green stone. Morning sunlight crossed the table while distant "
    "bells announced the arrival of another merchant ship in the quiet harbor.",
    "Across the courtyard, an apprentice counted jars of blue paint and carefully checked each lid. "
    "She planned to draw the northern coastline before winter closed the mountain roads. Her teacher "
    "had promised to explain how sailors measured distance without seeing land for weeks.",
    "A messenger appeared at the gate carrying an ordinary envelope with a purple seal. He waited "
    "patiently as the gardener collected fallen leaves from the path. Somewhere beyond the orchard, "
    "children were building a bridge from stones and arguing about which direction the river flowed.",
    "After lunch the travelers gathered their tools and walked toward the market square. They bought "
    "fresh bread, repaired a broken lantern, and asked a friendly baker about the weather. Nobody "
    "expected the afternoon journey to require anything more exciting than comfortable shoes and patience.",
)


def page(chapter=3224, title="First Thunder", identity=None, *, extra="", body=True):
    label = f"<p>Novel: {identity}</p>" if identity else ""
    prose = "".join(f"<p>{text}</p>" for text in PARAGRAPHS) if body else "<p>Coming soon</p>"
    return (f'<html><head><title>{chapter}{" " + title if title else ""}</title><link rel="canonical" href="https://rentry.co/{chapter}ss" />'
            f'</head><body><div class="entry-text"><section class="foreground-container"></section>'
            f'<article><div><p><a href="https://rentry.co/{chapter - 1}ss">Previous chapter</a></p>'
            f'{label}<h2>{chapter}{" " + title if title else ""}</h2>{extra}{prose}'
            f'<p><a href="https://rentry.co/{chapter + 1}ss">Next chapter</a></p>'
            '</div></article></div><h1 class="h3">Warning</h1></body></html>')


def placeholder(chapter=3500, message=""):
    return (f'<html><head><title>{chapter}</title>'
            f'<link rel="canonical" href="https://rentry.co/{chapter}ss"></head><body>'
            '<div class="entry-text"><article><div>'
            f'<p><a href="https://rentry.co/{chapter - 1}ss">{chapter - 1}ss</a></p>'
            f'<h1 id="{chapter}">{chapter}<a href="#{chapter}"></a></h1>{message}'
            f'<p><a href="https://rentry.co/{chapter + 1}ss">{chapter + 1}ss</a></p>'
            '</div></article></div><h1>Warning</h1></body></html>')


def response(status=200, html=None, url="https://rentry.co/3224ss", content_type="text/html"):
    result = Mock(spec=requests.Response)
    result.status_code, result.url, result.is_redirect = status, url, 300 <= status < 400
    result.headers = {"Content-Type": content_type}
    result.encoding = "utf-8"
    result.iter_content.return_value = [(page() if html is None else html).encode()]
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError("untrusted body", response=result)
    return result


def watching_state():
    state = initial_state()
    state.update(latest_seen=3223, latest_webnovel=3224, mode="watch_free_sites",
                 target_chapter=3224, target_title="First Thunder")
    return state


class TargetPageTests(unittest.TestCase):
    def check(self, html=None, chapter=3224, title="First Thunder"):
        with patch.object(parsers, "fetch_html", return_value=page() if html is None else html) as fetch:
            report = parsers.check_public_site(ENABLED, None, chapter, title)
        fetch.assert_called_once_with(ENABLED, f"https://rentry.co/{chapter}ss", allow_redirects=False)
        return report

    def test_configuration_appends_enabled_source(self):
        validate_source_config()
        self.assertEqual((CONFIG.name, CONFIG.url, CONFIG.enabled, CONFIG.allowed_hosts),
                         ("Rentry", "https://rentry.co/", True, ("rentry.co",)))
        self.assertEqual(PUBLIC_SITE_ORDER["Rentry"], 12)
        self.assertEqual(sum(site.enabled for site in PUBLIC_SITES[:-1]), 10)

    def test_generates_only_requested_canonical_candidate(self):
        for chapter, title in ((3224, "First Thunder"), (3225, "The Fourth Warrior")):
            with self.subTest(chapter=chapter):
                report = self.check(page(chapter, title), chapter, title)
                self.assertEqual((report.source, report.chapter, report.title, report.url),
                                 ("Rentry", chapter, title, f"https://rentry.co/{chapter}ss"))
                self.assertIsNone(report.position_chapter)

    def test_invalid_target_never_requests(self):
        for value in (True, False, "3224", 3224.0, 0, -1, 10001):
            with self.subTest(value=value), patch.object(parsers, "fetch_html") as fetch:
                with self.assertRaises(parsers.ParseError):
                    parsers.check_public_site(ENABLED, None, value, "First Thunder")
                fetch.assert_not_called()

    def test_no_target_skips_without_fetch(self):
        with patch.object(parsers, "fetch_html") as fetch:
            self.assertIsNone(parsers.check_public_site(ENABLED))
        fetch.assert_not_called()
        soup = BeautifulSoup('<a href="/chapter-9999">Chapter 9999</a>', "html.parser")
        self.assertEqual(parsers.iter_public_candidates(soup, CONFIG.url, CONFIG.name), [])

    def test_number_range_boundaries(self):
        self.assertEqual(parsers.rentry_chapter_url(1), "https://rentry.co/1ss")
        self.assertEqual(parsers.rentry_chapter_url(10000), "https://rentry.co/10000ss")

    def test_live_layout_without_label_requires_authoritative_title(self):
        self.assertEqual(self.check(page(identity=None)).chapter, 3224)
        for html, title in ((page(title=None), "First Thunder"),
                            (page(), None), (page(), "Other Title")):
            with self.subTest(title=title), self.assertRaises(parsers.ParseError):
                self.check(html, title=title)

    def test_rejects_unrelated_empty_placeholder_wrong_series_and_challenge(self):
        cases = ("", "<p>Coming soon</p>", "<title>3224 Rentry</title><p>Chapter 3224</p>",
                 page(identity="Other Novel"), page(body=False),
                 '<title>Just a moment...</title><form id="challenge-form"></form>' + page(),
                 page().replace("<h2>3224 First Thunder</h2>", "<p>Someone mentioned Chapter 3224</p>"))
        for html in cases:
            with self.subTest(html=html[:60]), self.assertRaises(parsers.ParseError):
                self.check(html)

    def test_rejects_conflicting_ambiguous_and_missing_headings(self):
        for html in (page(3225).replace("3225ss", "3224ss"), page(extra="<h2>3225 Other</h2>"),
                     page(extra="<h2>3224 First Thunder</h2>"), page(extra="<h3>Other Novel</h3>"),
                     page(title="or 3225 First Thunder"), page(title="Coming soon")):
            with self.subTest(html=html[:60]), self.assertRaises(parsers.ParseError):
                self.check(html)

    def test_title_conflicts_fail_but_normalization_and_missing_title_work(self):
        with self.assertRaises(parsers.ParseError):
            self.check(page(title="Another Story"))
        self.assertEqual(self.check(page(title="FIRST   THUNDER")).chapter, 3224)
        self.assertIsNone(self.check(page(title=None, identity="Shadow Slave")).title)
        self.assertEqual(self.check(page(identity="Shadow Slave"), title=None).chapter, 3224)
        with self.assertRaises(parsers.ParseError):
            self.check(page(title="X" * 200), title="X" * 180)

    def test_canonical_link_and_container_are_required_and_unambiguous(self):
        for html in (page().replace('href="https://rentry.co/3224ss"', 'href="https://rentry.co/3225ss"'),
                     page().replace('rel="canonical"', 'rel="alternate"'),
                     page().replace('</head>', '<link rel="canonical" href="https://rentry.co/3224ss"></head>'),
                     page().replace('class="entry-text"', 'class="unrelated"'), page() + page()):
            with self.subTest(html=html[:60]), self.assertRaises(parsers.ParseError):
                self.check(html)

    def test_hidden_evidence_and_script_prose_do_not_count(self):
        for html in (page(title=None, identity="Shadow Slave").replace('<p>Novel:', '<p hidden>Novel:'),
                     page().replace('<h2>', '<h2 style="display:none">'),
                     page().replace('<h2>', '<h2 aria-hidden="true">'),
                     page(body=False).replace('Coming soon', '<script>' + ' '.join(PARAGRAPHS) + '</script>'),
                     page(body=False).replace('Coming soon', 'placeholder ' * 200)):
            with self.subTest(html=html[:60]), self.assertRaises(parsers.ParseError):
                self.check(html)

    def test_observed_placeholder_and_short_messages_are_unavailable(self):
        for message in ("", "<p>Coming soon</p>", "<p>Work in progress.</p>",
                        "<p>Coming soon</p>" * 400,
                        "<p>" + "Coming soon. " * 400 + "</p>"):
            with self.subTest(length=len(message)):
                self.assertIsNone(self.check(placeholder(message=message), 3500, "Future Title"))
        # A bare numbered h2 also occurs in the published article structure.
        self.assertIsNone(self.check(placeholder().replace("h1", "h2"), 3500, "Future Title"))

    def test_short_or_boilerplate_content_cannot_be_a_published_report(self):
        for body in ("<p>Coming soon</p>" * 400,
                     "<p>" + PARAGRAPHS[0] + "</p>",
                     "".join(f"<p>{PARAGRAPHS[0]}</p>" for _ in range(100))):
            html = page(body=False).replace("<p>Coming soon</p>", body)
            with self.subTest(length=len(body)), self.assertRaises(parsers.ParseError):
                self.check(html)

    def test_contradictory_or_unfamiliar_templates_remain_errors(self):
        cases = (
            placeholder().replace('>3500<a', '>3501<a'),
            placeholder(message='<p>Novel: Other Novel</p>'),
            placeholder(message='<h3>Other Novel</h3>'),
            placeholder(message=''.join(f'<p>{p}</p>' for p in PARAGRAPHS)),
            placeholder(message='<p>' + PARAGRAPHS[0] * 8 + '</p>'),
            placeholder(message='<div>Unexpected unwrapped text</div>'),
            placeholder().replace('https://rentry.co/3501ss', 'https://evil.example/3501ss'),
            placeholder().replace('<title>3500</title>', '<title>Other Novel</title>'),
            placeholder().replace('rel="canonical"', 'rel="alternate"'),
            placeholder().replace('<article>', '<article><form><input type="password"></form>'),
        )
        for index, html in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(parsers.ParseError):
                self.check(html, 3500, "Future Title")

    def test_document_title_conflicts_and_hidden_prose_fail_closed(self):
        for html in (page().replace('<title>3224 First Thunder</title>', '<title>3225 Another</title>'),
                     page().replace('<article>', '<article hidden>'),
                     page().replace('<article>', '<article style="display:none">'),
                     page().replace('<article>', '<article><form><input type="password"></form>'),
                     page().replace('<h2>', '<h1>').replace('</h2>', '</h1>'),
                     page(body=False).replace('Coming soon', '<span hidden>' + ' '.join(PARAGRAPHS) + '</span>'),
                     page(body=False).replace('Coming soon', '<nav>' + ' '.join(PARAGRAPHS) + '</nav>')):
            with self.subTest(length=len(html)), self.assertRaises(parsers.ParseError):
                self.check(html)

    def test_body_thresholds_and_prose_position_remain_strict(self):
        bodies = (
            ''.join(f'<p>{p}</p>' for p in PARAGRAPHS[:2]),
            ''.join(f'<p>{"ordinary words " * 80}{i}</p>' for i in range(4)),
            ''.join(f'<p>{p}{"!" * 500}</p>' for p in PARAGRAPHS),
        )
        for body in bodies:
            with self.subTest(length=len(body)), self.assertRaises(parsers.ParseError):
                self.check(page(body=False).replace('<p>Coming soon</p>', body))
        prose = ''.join(f'<p>{p}</p>' for p in PARAGRAPHS)
        html = page(body=False).replace('<h2>', prose + '<h2>')
        with self.assertRaises(parsers.ParseError):
            self.check(html)

    def test_navigation_and_warning_text_cannot_supply_prose(self):
        prose = ''.join(f'<p>{p}</p>' for p in PARAGRAPHS)
        for html in (page(body=False) + prose,
                     page(body=False).replace('<article>', '<article><nav>' + prose + '</nav>'),
                     page(body=False).replace('<p>Coming soon</p>', '<aside>' + prose + '</aside>'),
                     page(body=False).replace('Next chapter', ' '.join(PARAGRAPHS))):
            with self.subTest(length=len(html)), self.assertRaises(parsers.ParseError):
                self.check(html)

    def test_current_production_state_validates_without_modification(self):
        state_path = Path(__file__).resolve().parents[1] / "state" / "state.json"
        before = state_path.read_bytes()
        validate_state(json.loads(before))
        self.assertEqual(state_path.read_bytes(), before)


class TargetHttpTests(unittest.TestCase):
    def test_rejects_noncanonical_urls_before_request(self):
        urls = ("http://rentry.co/3224ss", "https://www.rentry.co/3224ss", "https://evil.example/3224ss",
                "https://user@rentry.co/3224ss", "https://rentry.co:443/3224ss", "https://rentry.co/3224ss/",
                "https://rentry.co/3224ss?x=1", "https://rentry.co/3224ss#x", "https://rentry.co/3224ss?",
                "https://rentry.co/%33%32%32%34ss", "https://rentry.co/03224ss", "https://rentry.co/0ss",
                "https://rentry.co/10001ss", "https://rentry.co/3224ss;anything", "https://rentry.co/")
        for url in urls:
            with self.subTest(url=url), patch("requests.Session.get") as get:
                with self.assertRaises(HttpFetchError):
                    fetch_html(ENABLED, url)
                get.assert_not_called()
                with self.assertRaises(parsers.ParseError):
                    parsers.parse_rentry_chapter_page(page(), url, 3224, "First Thunder")

    def test_rejects_all_redirects_and_unexpected_final_url(self):
        for destination in ("https://evil.example/", "https://rentry.co/3225ss", "https://rentry.co/3224ss"):
            redirected = response(302)
            redirected.headers["Location"] = destination
            with self.subTest(destination=destination), patch("requests.Session.get", return_value=redirected) as get:
                with self.assertRaises(HttpFetchError):
                    parsers.check_public_site(ENABLED, None, 3224, "First Thunder")
                self.assertEqual(get.call_count, 1)
        with patch("requests.Session.get", return_value=response(404, url="https://rentry.co/other")):
            with self.assertRaises(HttpFetchError):
                parsers.check_public_site(ENABLED, None, 3224, "First Thunder")

    def test_content_type_and_response_size_bounds(self):
        for content_type in ("", "application/json", "text/plain", "application/xml"):
            with self.subTest(content_type=content_type), patch("requests.Session.get", return_value=response(content_type=content_type)):
                with self.assertRaises(HttpFetchError):
                    parsers.check_public_site(ENABLED, None, 3224, "First Thunder")
        with patch("requests.Session.get", return_value=response()), patch("shadow_slave_monitor.http_client.MAX_HTML_BYTES", 10):
            with self.assertRaisesRegex(HttpFetchError, "response_too_large"):
                parsers.check_public_site(ENABLED, None, 3224, "First Thunder")

    def test_only_genuine_target_404_is_clean_unavailable(self):
        with patch("requests.Session.get", return_value=response(404)) as get:
            self.assertIsNone(parsers.check_public_site(ENABLED, None, 3224, "First Thunder"))
        self.assertEqual(get.call_count, 1)
        with patch.object(parsers, "fetch_html", side_effect=requests.HTTPError("404 without response")):
            with self.assertRaises(requests.HTTPError):
                parsers.check_public_site(ENABLED, None, 3224, "First Thunder")

    def test_access_denial_rate_limit_server_errors_and_network_retries(self):
        for status in (403, 429, 500, 502, 503, 504):
            with self.subTest(status=status), patch("requests.Session.get", return_value=response(status)) as get, patch("time.sleep"):
                with self.assertRaises(requests.HTTPError):
                    parsers.check_public_site(ENABLED, None, 3224, "First Thunder")
                self.assertEqual(get.call_count, 1 if status == 403 else 3)
        for error in (requests.Timeout, requests.ConnectionError):
            with self.subTest(error=error), patch("requests.Session.get", side_effect=error("private")) as get, patch("time.sleep"):
                with self.assertRaises(error):
                    parsers.check_public_site(ENABLED, None, 3224, "First Thunder")
                self.assertEqual(get.call_count, 3)


class TargetMonitorTests(unittest.TestCase):
    def test_disabled_source_is_not_dispatched(self):
        with patch.object(monitor, "PUBLIC_SITES", (replace(CONFIG, enabled=False),)), patch.object(monitor, "check_public_site") as check:
            monitor.check_public_sites(RunResult(), expected_chapter=3224, expected_title="First Thunder")
        check.assert_not_called()

    def test_no_context_is_clean_skip_before_suppression(self):
        failures = {"Rentry": 3}
        with patch.object(monitor, "PUBLIC_SITES", (ENABLED,)), patch.object(monitor, "check_public_site") as check:
            result = RunResult()
            self.assertEqual(monitor.check_public_sites(result, failures), [])
        check.assert_not_called()
        self.assertEqual(failures, {"Rentry": 3})
        self.assertEqual(result.status, Health.HEALTHY)

    def test_repeated_missing_target_never_suppresses_and_later_detects(self):
        failures, positions = {}, {}
        with patch.object(monitor, "PUBLIC_SITES", (ENABLED,)), \
             patch.object(monitor, "public_source_recovery_window_open", return_value=False), \
             patch("requests.Session.get", return_value=response(404)) as get:
            for _ in range(8):
                result = RunResult()
                self.assertEqual(monitor.check_public_sites(result, failures, positions, 3224, "First Thunder"), [])
                self.assertEqual(result.status, Health.HEALTHY)
                self.assertEqual(failures, {})
                self.assertEqual(positions, {})
            self.assertEqual(get.call_count, 8)
        with patch.object(monitor, "PUBLIC_SITES", (ENABLED,)), patch("requests.Session.get", return_value=response()):
            reports = monitor.check_public_sites(RunResult(), failures, positions, 3224, "First Thunder")
        self.assertEqual([r.chapter for r in reports], [3224])

    def test_placeholders_keep_checking_then_publish_without_state_writes(self):
        state = watching_state()
        original = copy.deepcopy(state)
        with patch.object(monitor, "PUBLIC_SITES", (CONFIG,)), \
             patch.object(monitor, "public_source_recovery_window_open", return_value=False), \
             patch("requests.Session.get", return_value=response(html=placeholder(3224))) as get, \
             patch.object(monitor, "load_state", return_value=(state, False)), \
             patch.object(monitor, "save_state") as save, patch.object(monitor, "write_result"), \
             patch.object(monitor, "send_new_chapter") as send:
            for _ in range(8):
                monitor.main()
            self.assertEqual(get.call_count, 8)
            save.assert_not_called()
            send.assert_not_called()
        self.assertEqual(state, original)
        with patch.object(monitor, "PUBLIC_SITES", (CONFIG,)), \
             patch("requests.Session.get", return_value=response()), \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.run_watch_free_sites(state, RunResult())
        send.assert_called_once()
        self.assertEqual(state["latest_seen"], 3224)
        self.assertEqual(state["public_source_failures"], {})
        self.assertEqual(state["source_positions"], {})

    def test_placeholder_recovers_failure_count_and_diagnostic_is_unavailable(self):
        failures = {"Rentry": 3}
        with patch.object(monitor, "PUBLIC_SITES", (CONFIG,)), \
             patch.object(monitor, "public_source_recovery_window_open", return_value=True), \
             patch("requests.Session.get", return_value=response(html=placeholder(3224))):
            result = RunResult()
            self.assertEqual(monitor.check_public_sites(result, failures, expected_chapter=3224), [])
        self.assertEqual(failures, {})
        self.assertEqual(result.status, Health.HEALTHY)
        with patch.object(diagnose_source, "load_state", return_value=(watching_state(), False)), \
             patch("requests.Session.get", return_value=response(html=placeholder(3224))), \
             self.assertLogs(level="INFO") as logs:
            self.assertEqual(diagnose_source.diagnose_source("Rentry"), 0)
        self.assertIn("TARGET_NOT_AVAILABLE", "\n".join(logs.output))

    def test_clean_404_recovers_prior_failure_and_genuine_errors_are_isolated(self):
        other = ChapterReport(OTHER.name, 3224, "First Thunder", OTHER.url)
        for status in (404, 403, 429, 503, 200):
            failures = {"Rentry": 3}
            def check(site, *args):
                return other if site.name == OTHER.name else parsers.check_public_site(site, *args)
            with self.subTest(status=status), patch.object(monitor, "PUBLIC_SITES", (ENABLED, OTHER)), \
                 patch.object(monitor, "check_public_site", side_effect=check), \
                 patch("requests.Session.get", return_value=response(status, html=page(identity="Other Novel"))), patch("time.sleep"):
                reports = monitor.check_public_sites(RunResult(), failures, expected_chapter=3224, expected_title="First Thunder")
            self.assertEqual(reports, [other])
            self.assertEqual(failures, {} if status == 404 else {"Rentry": 4})

    def test_recovery_probe_404_clears_suppression(self):
        failures = {"Rentry": 4}
        with patch.object(monitor, "PUBLIC_SITES", (ENABLED,)), \
             patch.object(monitor, "public_source_recovery_window_open", return_value=True), \
             patch("requests.Session.get", return_value=response(404)):
            result = RunResult()
            monitor.check_public_sites(result, failures, expected_chapter=3224)
        self.assertEqual(failures, {})
        self.assertEqual(result.status, Health.HEALTHY)

    def test_order_aggregation_notification_and_deduplication(self):
        state = watching_state()
        other = ChapterReport(OTHER.name, 3224, "First Thunder", OTHER.url)
        def check(site, *args):
            return other if site.name == OTHER.name else parsers.check_public_site(site, *args)
        with patch.object(monitor, "PUBLIC_SITES", (ENABLED, OTHER)), \
             patch.object(monitor, "check_public_site", side_effect=check), \
             patch.object(parsers, "fetch_html", return_value=page()) as fetch, \
             patch.object(monitor, "send_new_chapter") as send, \
             patch.object(monitor, "check_webnovel", return_value=ChapterReport("WebNovel", 3224, "First Thunder", "")), \
             patch.object(monitor, "webnovel_due", return_value=True):
            monitor.run_watch_free_sites(state, RunResult())
            monitor.run_watch_webnovel(state, RunResult())
        fetch.assert_called_once_with(ENABLED, "https://rentry.co/3224ss", allow_redirects=False)
        send.assert_called_once()
        aggregate = send.call_args.args[2]
        self.assertEqual(aggregate.source, f"{OTHER.name}, Rentry")
        body = notification_body(3223, aggregate)
        self.assertIn("SOURCES:", body)
        self.assertEqual(aggregate.url, other.url)
        self.assertTrue(body.endswith("Rentry [https://rentry.co/3224ss]"))
        self.assertIn(f"{OTHER.name} [{OTHER.url}]", body)
        self.assertEqual(state["latest_seen"], 3224)
        self.assertEqual(state["mode"], "watch_webnovel")

    def test_unconfirmed_target_never_fetches_or_notifies(self):
        state = watching_state()
        state.update(mode="watch_webnovel", latest_webnovel=3223, target_chapter=None, target_title=None)
        with patch.object(monitor, "check_webnovel", return_value=ChapterReport("WebNovel", 3223, None, "")), \
             patch.object(monitor, "webnovel_due", return_value=True), \
             patch.object(parsers, "fetch_html") as fetch, patch.object(monitor, "send_new_chapter") as send:
            monitor.run_watch_webnovel(state, RunResult())
        fetch.assert_not_called()
        send.assert_not_called()

    def test_missing_target_does_not_change_state_or_commit(self):
        state = watching_state()
        original = copy.deepcopy(state)
        with patch.object(monitor, "PUBLIC_SITES", (ENABLED,)), \
             patch("requests.Session.get", return_value=response(404)), \
             patch.object(monitor, "load_state", return_value=(state, False)), \
             patch.object(monitor, "save_state") as save, patch.object(monitor, "write_result"), \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.main()
        save.assert_not_called()
        send.assert_not_called()
        self.assertEqual(state, original)
        self.assertEqual(set(state), set(initial_state()))

    def test_first_setup_without_target_context_does_not_fetch_or_notify(self):
        state = initial_state()
        official = ChapterReport("WebNovel", 3224, "First Thunder", "https://www.webnovel.com/")
        with patch.object(monitor, "PUBLIC_SITES", (ENABLED,)), patch.object(monitor, "check_webnovel", return_value=official), \
             patch.object(parsers, "fetch_html") as fetch, patch.object(monitor, "send_new_chapter") as send:
            result = RunResult()
            monitor.first_setup(state, result)
        fetch.assert_not_called()
        send.assert_not_called()
        self.assertEqual(result.status, Health.HEALTHY)
        self.assertEqual(state["latest_seen"], 3224)
        self.assertEqual(state["public_source_failures"], {})

    def test_pending_delivery_retains_source_and_retries_once(self):
        state = watching_state()
        with patch.object(monitor, "PUBLIC_SITES", (ENABLED,)), patch.object(parsers, "fetch_html", return_value=page()), \
             patch.object(monitor, "send_new_chapter", side_effect=NotificationDeliveryError("http_error", 503)) as send:
            monitor.run_watch_free_sites(state, RunResult())
        self.assertEqual(state["latest_seen"], 3223)
        self.assertEqual(state["pending_notification"]["sources"], ["Rentry"])
        with patch.object(monitor, "pending_due", return_value=True), patch.object(monitor, "send_new_chapter") as send, \
             patch.object(parsers, "fetch_html") as fetch:
            monitor.run_watch_free_sites(state, RunResult())
        send.assert_called_once()
        fetch.assert_not_called()
        self.assertIsNone(state["pending_notification"])
        self.assertEqual(state["latest_seen"], 3224)

    def test_failed_notification_reconstructs_chapter_links_without_new_state_fields(self):
        pending_fields = {
            "previous_seen", "first_pending_chapter", "latest_pending_chapter", "title", "sources", "url",
            "created_at", "first_failure_at", "last_attempt_at", "next_retry_at", "attempt_count",
            "last_error_category", "last_http_status",
        }
        for sources, expected_lines, url in (
            ("Rentry", ["Rentry [https://rentry.co/3225ss]"], "https://rentry.co/"),
            (f"Rentry, {OTHER.name}, Rentry",
             [f"{OTHER.name} [{OTHER.url}]", "Rentry [https://rentry.co/3225ss]"], OTHER.url),
        ):
            with self.subTest(sources=sources), patch.dict(monitor.os.environ, {"NTFY_NEWCHAPTER": "topic"}):
                state = watching_state()
                state.update(latest_seen=3224, latest_webnovel=3225, target_chapter=3225,
                             target_title="Chapter Title")
                report = ChapterReport(sources, 3225, "Chapter Title", url)
                with patch("shadow_slave_monitor.notifications._post_ntfy",
                           side_effect=NotificationDeliveryError("http_error", 503)) as failed_post:
                    monitor.queue_or_send(state, report, RunResult())
                failed_post.assert_called_once()
                self.assertEqual(state["latest_seen"], 3224)
                pending = state["pending_notification"]
                self.assertEqual(set(pending), pending_fields)
                self.assertEqual(pending["url"], url)
                self.assertEqual(pending["attempt_count"], 1)
                self.assertEqual(set(state), set(initial_state()))
                # Simulate the existing JSON persistence and validation boundary.
                state = validate_state(json.loads(json.dumps(state)))
                with patch.object(monitor, "pending_due", return_value=False), \
                     patch("shadow_slave_monitor.notifications._post_ntfy") as post:
                    monitor.try_deliver_pending(state, RunResult())
                post.assert_not_called()
                self.assertEqual(state["pending_notification"], pending)
                with patch.object(monitor, "pending_due", return_value=True), \
                     patch("shadow_slave_monitor.notifications._post_ntfy") as post, \
                     patch.object(monitor, "check_public_sites") as check:
                    monitor.run_watch_free_sites(state, RunResult())
                    monitor.try_deliver_pending(state, RunResult())
                post.assert_called_once_with(*failed_post.call_args.args)
                self.assertEqual(post.call_args.args[2].splitlines()[4:], expected_lines)
                check.assert_not_called()
                self.assertIsNone(state["pending_notification"])
                self.assertEqual((state["latest_seen"], state["latest_url"], state["mode"]),
                                 (3225, url, "watch_webnovel"))

    def test_diagnostic_clean_unavailable_and_missing_context(self):
        for state, expected_code in ((watching_state(), "TARGET_NOT_AVAILABLE"), (initial_state(), "TARGET_CONTEXT_MISSING")):
            with patch.object(diagnose_source, "load_state", return_value=(state, False)), \
                 patch("requests.Session.get", return_value=response(404)), self.assertLogs(level="INFO") as logs:
                self.assertEqual(diagnose_source.diagnose_source("Rentry"), 0)
            self.assertIn(expected_code, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
