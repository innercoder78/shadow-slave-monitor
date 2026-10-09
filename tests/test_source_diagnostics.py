from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import Mock, patch

import requests

from shadow_slave_monitor import diagnose_source, monitor, parsers, state_manager
from shadow_slave_monitor.config import PUBLIC_SITES, SourceConfig
from shadow_slave_monitor.diagnostics import (
    HtmlDocument, PARSE_CODES, ResponseMetadata, diagnostic_summary,
)
from shadow_slave_monitor.http_client import HttpFetchError, fetch_html, safe_exception_details
from shadow_slave_monitor.models import ChapterReport, Health, RunResult
from shadow_slave_monitor.parsers import ParseError, check_public_site, parse_lightnovelup_chapter_page


CHALLENGE = '<title>Just a moment...</title><form id="challenge-form">private body token=secret</form>'


def response(status=200, html='<html>private body token=secret</html>', *, url='https://example.com/private?token=secret'):
    result = Mock(spec=requests.Response)
    result.status_code = status
    result.url = url
    result.is_redirect = False
    result.headers = {'Content-Type': 'text/html'}
    result.encoding = 'utf-8'
    result.iter_content.return_value = [html.encode()]
    if status >= 400:
        request = requests.Request('GET', url, headers={'Authorization': 'secret'}).prepare()
        result.raise_for_status.side_effect = requests.HTTPError('private exception cookie=secret', response=result, request=request)
    return result


class DiagnosticCodeTests(unittest.TestCase):
    source = SourceConfig('Test Source', 'https://example.com/private?token=secret', True, ('example.com',))

    def assert_safe(self, summary):
        self.assertLess(len(summary), 1400)
        self.assertNotIn('\n', summary)
        for forbidden in ('private', 'secret', 'cookie', 'Authorization', 'token=', 'https://', 'redesign', 'bot blocking'):
            self.assertNotIn(forbidden, summary)

    def test_http_status_codes_and_retry_counts(self):
        for status in (403, 429, 500, 502, 503, 504, 404):
            with self.subTest(status=status), patch('requests.Session.get', return_value=response(status)), patch('time.sleep'):
                with self.assertRaises(requests.HTTPError) as caught:
                    fetch_html(self.source)
                summary = diagnostic_summary(self.source, caught.exception)
                self.assertIn(f'code=HTTP_{status} stage=http status={status}', summary)
                self.assertIn('host=example.com', summary)
                self.assertIn('attempts=' + ('3' if status in (429, 500, 502, 503, 504) else '1'), summary)
                self.assertNotIn('CHALLENGE', summary)
                self.assert_safe(summary)

    def test_network_codes_have_no_invented_status(self):
        for error, code in ((requests.ReadTimeout, 'NETWORK_TIMEOUT'),
                            (requests.ConnectTimeout, 'NETWORK_TIMEOUT'),
                            (requests.ConnectionError, 'NETWORK_CONNECTION_ERROR'),
                            (requests.RequestException, 'NETWORK_REQUEST_ERROR')):
            with self.subTest(error=error), patch('requests.Session.get', side_effect=error('private exception cookie=secret')), patch('time.sleep'):
                with self.assertRaises(error) as caught:
                    fetch_html(self.source)
                summary = diagnostic_summary(self.source, caught.exception)
                self.assertIn('code=' + code, summary)
                self.assertNotIn('status=', summary)
                self.assertIn('attempts=' + ('1' if error is requests.RequestException else '3'), summary)
                self.assert_safe(summary)

    def test_failed_fetch_after_redirect_does_not_reuse_redirect_status(self):
        redirect = response(302)
        redirect.is_redirect = True
        redirect.headers = {'Location': '/next'}
        with patch('requests.Session.get', side_effect=[redirect, requests.Timeout('private secret')] * 3), patch('time.sleep'):
            with self.assertRaises(requests.Timeout) as caught:
                fetch_html(self.source)
        summary = diagnostic_summary(self.source, caught.exception)
        self.assertIn('code=NETWORK_TIMEOUT', summary)
        self.assertIn('attempts=3', summary)
        self.assertNotIn('status=', summary)

    def test_streaming_failure_retains_received_status(self):
        result = response(200)
        result.iter_content.side_effect = requests.ConnectionError('private secret')
        with patch('requests.Session.get', return_value=result), patch('time.sleep'):
            with self.assertRaises(requests.ConnectionError) as caught:
                fetch_html(self.source)
        summary = diagnostic_summary(self.source, caught.exception)
        self.assertIn('code=NETWORK_CONNECTION_ERROR', summary)
        self.assertIn('status=200', summary)
        self.assertIn('attempts=3', summary)
        self.assert_safe(summary)

    def test_http_error_without_response_is_unknown(self):
        summary = diagnostic_summary(self.source, requests.HTTPError('secret'))
        self.assertIn('code=HTTP_ERROR', summary)
        self.assertNotIn('status=', summary)
        self.assert_safe(summary)

    def test_unsafe_redirects_show_actual_status_without_location(self):
        for location in ('https://secret.evil.example/private?token=secret', 'http://example.com/private'):
            result = response(302)
            result.is_redirect = True
            result.headers = {'Location': location}
            with self.subTest(location=location), patch('requests.Session.get', return_value=result):
                with self.assertRaises(HttpFetchError) as caught:
                    fetch_html(self.source)
                summary = diagnostic_summary(self.source, caught.exception)
                self.assertIn('code=HTTP_UNSAFE_REDIRECT', summary)
                self.assertIn('status=302', summary)
                self.assertIn('attempts=1', summary)
                self.assert_safe(summary)
                result.close.assert_called()

    def test_missing_redirect_location_and_redirect_limit(self):
        for headers, reason in (({}, 'missing_redirect_location'),
                                ({'Location': '/next'}, 'redirect_limit')):
            result = response(302)
            result.is_redirect = True
            result.headers = headers
            with self.subTest(reason=reason), patch('requests.Session.get', return_value=result):
                with self.assertRaises(HttpFetchError) as caught:
                    fetch_html(self.source)
                self.assertEqual(caught.exception.reason, reason)
                summary = diagnostic_summary(self.source, caught.exception)
                self.assertIn('code=HTTP_UNSAFE_REDIRECT', summary)
                self.assertIn('status=302', summary)

    def test_content_type_and_size_policy_codes(self):
        result = response()
        result.headers['Content-Type'] = 'application/json; private=secret'
        with patch('requests.Session.get', return_value=result), self.assertRaises(HttpFetchError) as caught:
            fetch_html(self.source)
        summary = diagnostic_summary(self.source, caught.exception)
        self.assertIn('code=HTTP_UNSUPPORTED_CONTENT_TYPE stage=response status=200', summary)
        self.assert_safe(summary)
        with patch('requests.Session.get', return_value=response()), patch('shadow_slave_monitor.http_client.MAX_HTML_BYTES', 1):
            with self.assertRaises(HttpFetchError) as caught:
                fetch_html(self.source)
        self.assertIn('code=HTTP_RESPONSE_TOO_LARGE', diagnostic_summary(self.source, caught.exception))

    def test_initial_http_policy_rejection_has_zero_attempts(self):
        with patch('requests.Session.get') as get, self.assertRaises(HttpFetchError) as caught:
            fetch_html(self.source, 'http://example.com/private')
        get.assert_not_called()
        summary = diagnostic_summary(self.source, caught.exception)
        self.assertIn('attempts=0', summary)
        self.assertNotIn('status=', summary)

    def test_parser_code_mapping(self):
        for reason, code in PARSE_CODES.items():
            with self.subTest(reason=reason):
                self.assertEqual(ParseError(reason).code, code)
                self.assertIn('code=' + code, diagnostic_summary(self.source, ParseError(reason)))
        for reason in ('unknown private exception secret', 'blocked', 'site redesigned'):
            summary = diagnostic_summary(self.source, ParseError(reason))
            self.assertIn('code=PARSE_OTHER', summary)
            self.assert_safe(summary)

    def test_unexpected_exception_types_are_internal_without_leaking_text(self):
        for error_type in (RuntimeError, AttributeError, KeyError, TypeError):
            error = error_type('private body secret https://example.com/?token=secret\ntraceback')
            with self.subTest(error_type=error_type):
                summary = diagnostic_summary(self.source, error)
                self.assertIn('code=CHECK_INTERNAL_ERROR stage=internal', summary)
                self.assertNotIn('PARSE_OTHER', summary)
                self.assertNotIn('traceback', summary)
                self.assert_safe(summary)

    def test_reason_attributes_cannot_disguise_an_internal_error(self):
        for reason in ('chapter_heading_conflict', 'unexpected_content_type',
                       'next_link_noncanonical[categories=private secret]',
                       'Could not find any chapter links on private secret.'):
            error = RuntimeError('private secret')
            error.reason = reason
            error.challenge = True
            error.code = 'PAGE_CHALLENGE_SUSPECTED'
            with self.subTest(reason=reason):
                summary = diagnostic_summary(self.source, error)
                self.assertIn('code=CHECK_INTERNAL_ERROR stage=internal', summary)
                self.assertNotIn('CHALLENGE', summary)
                self.assert_safe(summary)

    def test_unknown_parse_error_stays_parse_other_with_response_metadata(self):
        error = ParseError('unknown private secret parser reason')
        error.status, error.host, error.attempts = 200, 'example.com', 1
        summary = diagnostic_summary(self.source, error)
        self.assertIn('code=PARSE_OTHER stage=parse status=200 host=example.com attempts=1', summary)
        self.assertNotIn('CHECK_INTERNAL_ERROR', summary)
        self.assert_safe(summary)

    def test_allowlisted_and_bounded_fields(self):
        error = ParseError('unknown private exception', counters={
            'links_inspected': 10**10, 'next_links': -10, 'private_token_secret': 9000,
            'ambiguous': 1, 'canonical_candidates': 'secret',
        })
        error.host = 'example.com\n::warning::private secret'
        error.attempts = 'secret'
        error.status = 'private secret'
        summary = diagnostic_summary(self.source, error)
        self.assertIn('links_inspected=9999', summary)
        self.assertIn('next_links=0', summary)
        self.assertNotIn('host=', summary)
        self.assertNotIn('status=', summary)
        self.assertNotIn('attempts=', summary)
        self.assert_safe(summary)
        policy_error = HttpFetchError('private exception cookie=secret', host='example.com\nprivate secret')
        self.assert_safe(diagnostic_summary(self.source, policy_error))
        self.assertEqual(safe_exception_details(policy_error), 'reason=http_policy_error')


class ParserDiagnosticTests(unittest.TestCase):
    source = next(site for site in PUBLIC_SITES if site.name == 'Chikari')
    navigation = next(site for site in PUBLIC_SITES if site.name == 'LightNovelUp')
    chapter_url = 'https://lightnovelup.com/novel/shadow-slave/chapter-3173-life-goes-on/'

    def failed_check(self, html, source=None):
        with patch('shadow_slave_monitor.parsers.fetch_html', return_value=html), self.assertRaises(ParseError) as caught:
            check_public_site(source or self.source)
        return caught.exception

    def test_successful_transport_metadata_reaches_parse_failure(self):
        result = response(200, '<p>private body secret</p>', url=self.source.url + '?token=secret')
        with patch('requests.Session.get', return_value=result), self.assertRaises(ParseError) as caught:
            check_public_site(self.source)
        summary = diagnostic_summary(self.source, caught.exception)
        self.assertIn('code=PARSE_NO_CHAPTER_LINKS stage=parse status=200', summary)
        self.assertIn('host=chikari.moe attempts=1', summary)
        self.assertNotIn('private', summary)
        self.assertNotIn('secret', summary)

    def test_fetch_html_remains_a_string_with_actual_metadata(self):
        result = response(201, '<p>ok</p>')
        with patch('requests.Session.get', return_value=result):
            html = fetch_html(DiagnosticCodeTests.source)
        self.assertIsInstance(html, str)
        self.assertEqual(html, '<p>ok</p>')
        self.assertEqual(html.metadata, ResponseMetadata(201, 'example.com', 1))

    def test_plain_mock_html_does_not_invent_metadata(self):
        error = self.failed_check('<p>ordinary page</p>')
        self.assertNotIn('status=', diagnostic_summary(self.source, error))
        self.assertNotIn('attempts=', diagnostic_summary(self.source, error))

    def test_chikari_structural_counts_preserve_url_rejection(self):
        html = ('<a href="/about">About</a>'
                '<a href="/novels/shadow-slave/title-only">private</a>'
                '<a href="https://evil.example/novels/shadow-slave/3173?token=secret">private</a>'
                '<a href="/novels/shadow-slave/3173?token=secret">private</a>')
        error = self.failed_check(html)
        self.assertEqual(error.counters, {
            'links_inspected': 4, 'series_path_links': 3, 'canonical_chapter_links': 0,
            'invalid_chapters': 0, 'challenge_indicators': 0,
        })
        summary = diagnostic_summary(self.source, error)
        self.assertIn('code=PARSE_NO_CHAPTER_LINKS', summary)
        self.assertNotIn('CHALLENGE', summary)
        self.assertNotIn('evil', summary)
        self.assertNotIn('private', summary)

    def test_invalid_canonical_chapter_has_validation_code(self):
        error = self.failed_check('<a href="/novels/shadow-slave/0">Chapter 0</a>')
        summary = diagnostic_summary(self.source, error)
        self.assertIn('code=PARSE_CHAPTER_INVALID stage=chapter_validation', summary)
        self.assertIn('invalid_chapters=1', summary)
        self.assertIn('canonical_chapter_links=1', summary)

    def test_recognizable_challenge_is_suspected_only(self):
        error = self.failed_check(CHALLENGE)
        summary = diagnostic_summary(self.source, error)
        self.assertEqual(error.code, 'PAGE_CHALLENGE_SUSPECTED')
        self.assertIn('code=PAGE_CHALLENGE_SUSPECTED', summary)
        self.assertIn('challenge_indicators=1', summary)
        self.assertNotIn('secret', summary)
        self.assertNotIn('private', summary)

    def test_insufficient_challenge_evidence_stays_parser_error(self):
        for html in ('<p>No chapters. Captcha or Cloudflare may be used.</p>',
                     '<title>Just a moment...</title><p>ordinary page</p>',
                     '<title>Shadow Slave</title><form id="challenge-form"></form>',
                     '<h1>Access denied</h1><p>403 forbidden bot blocked redesign</p>'):
            with self.subTest(html=html):
                error = self.failed_check(html)
                self.assertIn('code=PARSE_NO_CHAPTER_LINKS', diagnostic_summary(self.source, error))
                self.assertEqual(error.counters['challenge_indicators'], 0)

    def test_valid_chapters_with_challenge_words_are_still_accepted(self):
        html = CHALLENGE + '<a href="/novels/shadow-slave/3173">Chapter 3173</a>'
        with patch('shadow_slave_monitor.parsers.fetch_html', return_value=html):
            self.assertEqual(check_public_site(self.source).chapter, 3173)

    def navigation_failure(self, links, heading='<h1>Chapter 3173: Life Goes On</h1>'):
        html = heading + ''.join(f'<a href="{href}">Next Chapter</a>' for href in links)
        with self.assertRaises(ParseError) as caught:
            parse_lightnovelup_chapter_page(html, self.chapter_url)
        return caught.exception

    def test_rejected_next_categories_are_counted_without_leaking_hrefs(self):
        error = self.navigation_failure([
            '/novel/shadow-slave/chapter-future-title/',
            '/novel/shadow-slave/chapter-3174/',
            'https://user:secret@lightnovelup.com/novel/shadow-slave/chapter-3174-valid/',
            '/novel/shadow-slave/chapter-3174-valid/?token=secret',
        ])
        self.assertEqual(error.code, 'PARSE_NONCANONICAL_NEXT')
        self.assertEqual(error.counters['next_links'], 4)
        self.assertEqual(error.counters['canonical_candidates'], 0)
        for category in ('title_only_chapter_path', 'numeric_only_chapter_path', 'unexpected_authority', 'query_fragment_or_params'):
            self.assertEqual(error.counters['rejected_' + category], 1)
        summary = diagnostic_summary(self.navigation, error)
        for forbidden in ('future-title', 'user:', 'secret', 'token='):
            self.assertNotIn(forbidden, summary)

    def test_ambiguous_navigation_retains_rejected_counters(self):
        error = self.navigation_failure([
            '/novel/shadow-slave/chapter-3174-first/', '/novel/shadow-slave/chapter-3174-second/',
            '/novel/shadow-slave/chapter-future-title/',
        ], heading=CHALLENGE + '<h1>Chapter 3173: Life Goes On</h1>')
        self.assertEqual(error.code, 'PARSE_AMBIGUOUS_NEXT')
        self.assertEqual(error.counters['ambiguous'], 1)
        self.assertEqual(error.counters['canonical_candidates'], 2)
        self.assertEqual(error.counters['rejected_title_only_chapter_path'], 1)
        self.assertNotIn('PAGE_CHALLENGE_SUSPECTED', diagnostic_summary(self.navigation, error))

    def test_nonmonotonic_and_heading_mismatch_codes(self):
        error = self.navigation_failure(['/novel/shadow-slave/chapter-3175-jump/'])
        self.assertEqual(error.code, 'PARSE_NONMONOTONIC_NEXT')
        self.assertEqual(error.counters['nonmonotonic'], 1)
        self.assertEqual(error.counters['ambiguous'], 0)
        error = self.navigation_failure([], heading='<h1>Chapter 3172: private title secret</h1>')
        summary = diagnostic_summary(self.navigation, error)
        self.assertIn('code=PARSE_CHAPTER_MISMATCH stage=chapter_validation', summary)
        self.assertNotIn('private', summary)
        self.assertNotIn('secret', summary)

    def test_secondary_page_failure_uses_its_metadata(self):
        first = HtmlDocument('<h1>Chapter 3173</h1><a href="/novel/shadow-slave/chapter-3174-next/">Next</a>',
                             ResponseMetadata(200, 'lightnovelup.com', 1))
        second = HtmlDocument('<h1>Chapter 3172</h1>', ResponseMetadata(202, 'lightnovelup.com', 2))
        with patch('shadow_slave_monitor.parsers.fetch_html', side_effect=[first, second]), self.assertRaises(ParseError) as caught:
            check_public_site(self.navigation)
        summary = diagnostic_summary(self.navigation, caught.exception)
        self.assertIn('status=202', summary)
        self.assertIn('attempts=2', summary)

    def test_failed_secondary_fetch_does_not_reuse_success_status(self):
        first = HtmlDocument('<h1>Chapter 3173</h1><a href="/novel/shadow-slave/chapter-3174-next/">Next</a>',
                             ResponseMetadata(200, 'lightnovelup.com', 1))
        with patch('shadow_slave_monitor.parsers.fetch_html', side_effect=[first, requests.Timeout('private secret')]):
            with self.assertRaises(requests.Timeout) as caught:
                check_public_site(self.navigation)
        self.assertNotIn('status=', diagnostic_summary(self.navigation, caught.exception))

    def test_response_context_is_isolated_between_threads_and_checks(self):
        barrier = Barrier(2)
        sites = [SourceConfig('Source ' + str(status), f'https://example{status}.com', True, (f'example{status}.com',))
                 for status in (201, 202)]
        def fetch(site):
            return HtmlDocument('<p>No chapters</p>', ResponseMetadata(int(site.name[-3:]), site.allowed_hosts[0], 1))
        def candidates(*args):
            barrier.wait(timeout=5)
            return []
        def check(site):
            try:
                check_public_site(site)
            except ParseError as error:
                return diagnostic_summary(site, error)
        with patch.object(parsers, 'fetch_html', side_effect=fetch), patch.object(parsers, 'iter_public_candidates', side_effect=candidates):
            with ThreadPoolExecutor(max_workers=2) as executor:
                summaries = list(executor.map(check, sites))
        for site, summary in zip(sites, summaries):
            self.assertIn('status=' + site.name[-3:], summary)
            self.assertIn('host=' + site.allowed_hosts[0], summary)
        self.assertIsNone(parsers._response_metadata.get())

    def test_actual_large_page_counters_are_bounded(self):
        error = self.failed_check('<a href="/novels/shadow-slave/title-only">private</a>' * 10005)
        self.assertEqual(error.counters['links_inspected'], 9999)
        self.assertEqual(error.counters['series_path_links'], 9999)
        self.assertLess(len(diagnostic_summary(self.source, error)), 600)

    def test_internal_source_error_is_contained_with_accurate_metadata(self):
        good = SourceConfig('Good', 'https://good.example', True, ('good.example',))
        good_report = ChapterReport('Good', 3173, None, 'https://good.example/chapter-3173')
        real_check = check_public_site
        def check(site):
            return real_check(site) if site.name == self.source.name else good_report
        def candidates(*args):
            raise AttributeError('private body secret https://example.com/?token=secret')
        failures = {}
        result = RunResult()
        with patch.object(monitor, 'PUBLIC_SITES', (self.source, good)), \
             patch.object(monitor, 'check_public_site', side_effect=check), \
             patch.object(monitor, 'public_source_recovery_window_open', return_value=False), \
             patch.object(parsers, 'fetch_html', return_value=HtmlDocument('<p>private body</p>',
                               ResponseMetadata(200, 'chikari.moe', 1))), \
             patch.object(parsers, 'iter_public_candidates', side_effect=candidates), self.assertLogs(level='WARNING') as captured:
            self.assertEqual(monitor.check_public_sites(result, failures), [good_report])
        self.assertEqual(result.status, Health.DEGRADED)
        self.assertEqual(failures, {'Chikari': 1})
        output = '\n'.join(captured.output)
        self.assertIn('code=CHECK_INTERNAL_ERROR stage=internal status=200 host=chikari.moe attempts=1', output)
        self.assertEqual(output.count('Source check failed:'), 1)
        for forbidden in ('private', 'secret', 'token=', 'https://'):
            self.assertNotIn(forbidden, output)

    def test_one_failure_summary_does_not_stop_usable_sources(self):
        good = SourceConfig('Good', 'https://good.example', True, ('good.example',))
        result = RunResult()
        def check(site):
            if site.name == self.source.name:
                raise self.failed_check(CHALLENGE)
            return ChapterReport('Good', 3173, None, 'https://good.example/chapter-3173')
        with patch.object(monitor, 'PUBLIC_SITES', (self.source, good)), patch.object(monitor, 'check_public_site', side_effect=check), \
             patch.object(monitor, 'public_source_recovery_window_open', return_value=False), self.assertLogs(level='WARNING') as captured:
            reports = monitor.check_public_sites(result, {})
        self.assertEqual([report.source for report in reports], ['Good'])
        self.assertEqual(result.status, Health.DEGRADED)
        summaries = [line for line in captured.output if 'Source check failed:' in line]
        self.assertEqual(len(summaries), 1)
        self.assertIn('code=PAGE_CHALLENGE_SUSPECTED', summaries[0])


class ManualDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.state = state_manager.initial_state()
        self.state.update(latest_seen=3173, latest_webnovel=3174, target_chapter=3174,
                          mode='watch_free_sites', target_title='Next Title', latest_title='Previous Title')
        self.state['public_source_failures'] = {'Chikari': 4, 'LightNovelUp': 4}
        self.state['source_positions'] = {'LightNovelUp': {'chapter': 3173, 'url': ParserDiagnosticTests.chapter_url}}
        self.path.write_text(json.dumps(self.state), encoding='utf-8')
        self.original = self.path.read_bytes()

    def assert_unchanged(self):
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual([file.name for file in self.path.parent.iterdir()], ['state.json'])

    def test_manual_success_bypasses_suppression_and_does_not_notify_or_write(self):
        report = ChapterReport('LightNovelUp', 3174, 'private title secret', 'https://lightnovelup.com/private?token=secret')
        loaded = copy.deepcopy(self.state)
        def check(site, position, *context):
            self.assertEqual(site.name, 'LightNovelUp')
            self.assertEqual(context, (3174, 'Next Title', 3173, 'Previous Title'))
            position['chapter'] = 9999  # The diagnostic passes an isolated cursor copy.
            return report
        with patch.object(diagnose_source, 'load_state', return_value=(loaded, False)), \
             patch.object(diagnose_source, 'check_public_site', side_effect=check) as checker, \
             patch('shadow_slave_monitor.notifications.send_new_chapter') as notify, \
             patch.object(state_manager, 'save_state') as save, \
             patch.object(state_manager, 'atomic_write_json') as write, self.assertLogs(level='INFO') as captured:
            self.assertEqual(diagnose_source.diagnose_source('LightNovelUp', state_path=self.path), 0)
        checker.assert_called_once()
        notify.assert_not_called()
        save.assert_not_called()
        write.assert_not_called()
        self.assertEqual(loaded, self.state)
        self.assert_unchanged()
        output = '\n'.join(captured.output)
        self.assertIn('code=SOURCE_OK', output)
        self.assertNotIn('secret', output)
        self.assertNotIn('private', output)

    def test_manual_full_success_logs_only_safe_chapter_fields(self):
        html = ('<section><h2>Latest chapters</h2>'
                '<a href="/shadow-slave/chapter-3174.html">Chapter 3174: New Horizon</a></section><p>private body token=secret</p>')
        result = response(200, html, url='https://novelfull.com/shadow-slave.html')
        with patch('requests.Session.get', return_value=result), \
             patch('shadow_slave_monitor.notifications.send_new_chapter') as notify, \
             patch.object(state_manager, 'save_state') as save, self.assertLogs(level='INFO') as captured:
            self.assertEqual(diagnose_source.diagnose_source('NovelFull', state_path=self.path), 0)
        notify.assert_not_called()
        save.assert_not_called()
        self.assert_unchanged()
        summary = '\n'.join(captured.output)
        self.assertIn('code=SOURCE_OK', summary)
        self.assertIn('New Horizon', summary)
        self.assertIn('https://novelfull.com/shadow-slave/chapter-3174.html', summary)
        for forbidden in ('private', 'secret', 'token='):
            self.assertNotIn(forbidden, summary)

    def test_manual_real_parser_with_mocked_network_never_writes_or_notifies(self):
        result = response(200, CHALLENGE, url=ParserDiagnosticTests.source.url)
        with patch('requests.Session.get', return_value=result), \
             patch('shadow_slave_monitor.notifications.send_new_chapter') as notify, \
             patch.object(state_manager, 'save_state') as save, \
             patch.object(state_manager, 'atomic_write_json') as write, self.assertLogs(level='WARNING') as captured:
            self.assertEqual(diagnose_source.diagnose_source('Chikari', state_path=self.path), 0)
        notify.assert_not_called()
        save.assert_not_called()
        write.assert_not_called()
        self.assert_unchanged()
        self.assertIn('code=PAGE_CHALLENGE_SUSPECTED', '\n'.join(captured.output))

    def test_expected_source_failures_are_successful_workflow_outcomes(self):
        for error in (ParseError('chapter_heading_conflict'), ParseError('unknown private secret reason'),
                      HttpFetchError('redirect_limit'),
                      requests.Timeout('private secret'), requests.HTTPError('private secret')):
            with self.subTest(error=type(error).__name__), patch.object(diagnose_source, 'check_public_site', side_effect=error), \
                 self.assertLogs(level='WARNING') as captured:
                self.assertEqual(diagnose_source.diagnose_source('Chikari', state_path=self.path), 0)
            self.assertNotIn('secret', '\n'.join(captured.output))
            self.assert_unchanged()

    def test_manual_internal_errors_fail_without_notifications_or_state_changes(self):
        for error_type in (RuntimeError, AttributeError, KeyError, TypeError):
            with self.subTest(error_type=error_type), \
                 patch.object(diagnose_source, 'check_public_site', side_effect=error_type('private secret traceback')), \
                 patch('shadow_slave_monitor.notifications.send_new_chapter') as notify, \
                 patch.object(state_manager, 'save_state') as save, \
                 patch.object(state_manager, 'atomic_write_json') as write, self.assertLogs(level='ERROR') as captured:
                self.assertEqual(diagnose_source.diagnose_source('Chikari', state_path=self.path), 2)
            notify.assert_not_called()
            save.assert_not_called()
            write.assert_not_called()
            self.assert_unchanged()
            output = '\n'.join(captured.output)
            self.assertIn('code=DIAGNOSTIC_INTERNAL_ERROR stage=internal', output)
            for forbidden in ('private', 'secret', 'traceback', 'PARSE_OTHER'):
                self.assertNotIn(forbidden, output)

    def test_invalid_names_and_unsafe_input_never_check_network_or_echo_input(self):
        for name in ('', 'chikari', ' Chikari ', 'WebNovel', 'Chikari; touch secret',
                     '$(touch secret)', 'Chikari\n::warning::secret', '"; echo secret; #'):
            with self.subTest(name=name), patch.object(diagnose_source, 'check_public_site') as checker, \
                 patch.object(diagnose_source, 'load_state') as load, self.assertLogs(level='ERROR') as captured:
                self.assertEqual(diagnose_source.diagnose_source(name, state_path=self.path), 2)
            checker.assert_not_called()
            load.assert_not_called()
            self.assertNotIn('secret', '\n'.join(captured.output))
            self.assert_unchanged()

    def test_main_reads_workflow_input_only_as_environment_data(self):
        with patch.dict('os.environ', {'SOURCE_NAME': '$(touch secret)'}), \
             patch.object(diagnose_source, 'diagnose_source', return_value=2) as check:
            self.assertEqual(diagnose_source.main(), 2)
        check.assert_called_once_with('$(touch secret)')

    def test_invalid_state_and_unexpected_errors_fail_safely(self):
        self.path.write_text('{invalid secret', encoding='utf-8')
        with patch.object(diagnose_source, 'check_public_site') as checker, self.assertLogs(level='ERROR') as captured:
            self.assertEqual(diagnose_source.diagnose_source('Chikari', state_path=self.path), 2)
        checker.assert_not_called()
        self.assertNotIn('secret', '\n'.join(captured.output))
        self.path.write_bytes(self.original)
        with patch.object(diagnose_source, 'check_public_site', side_effect=RuntimeError('private secret')), self.assertLogs(level='ERROR') as captured:
            self.assertEqual(diagnose_source.diagnose_source('Chikari', state_path=self.path), 2)
        self.assertNotIn('secret', '\n'.join(captured.output))
        self.assert_unchanged()

    def test_workflow_uses_env_and_selected_checkout_without_side_effect_steps(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/source-diagnostics.yml').read_text(encoding='utf-8')
        self.assertIn('workflow_dispatch:', workflow)
        self.assertIn('SOURCE_NAME: ${{ inputs.source_name }}', workflow)
        self.assertIn('run: python -m shadow_slave_monitor.diagnose_source', workflow)
        self.assertIn('python-version: "3.12"', workflow)
        self.assertIn('--require-hashes -r dependencies/requirements.lock', workflow)
        self.assertIn('persist-credentials: false', workflow)
        for forbidden in ('schedule:', 'secrets.', 'contents: write', 'git commit', 'git push', 'ref: main',
                          'upload-artifact', 'shadow_slave_monitor.monitor', 'shadow_slave_monitor.watchdog'):
            self.assertNotIn(forbidden, workflow)
        self.assertNotIn('${{ inputs.', '\n'.join(line for line in workflow.splitlines() if 'run:' in line))


if __name__ == '__main__':
    unittest.main()
