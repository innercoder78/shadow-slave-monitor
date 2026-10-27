"""Offline integration coverage of the standalone runner diagnostic."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import logging
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch

import requests
from bs4 import BeautifulSoup

from shadow_slave_monitor import diagnose_runner as diagnostic, parsers, state_manager
from shadow_slave_monitor.config import HEADERS, PUBLIC_SITES, WEBNOVEL_SOURCE
from shadow_slave_monitor.models import ChapterReport
from shadow_slave_monitor.notifications import pending_from_report


ROOT = Path(__file__).resolve().parents[1]
SITE = next(source for source in PUBLIC_SITES if source.name == "Telegram")
PRIVATE = "private-secret https://untrusted.example/private?token=secret"
OFFICIAL = '<html><div>Latest Release</div><div>Chapter 3227: Private Title</div></html>'
GROUPED = (ROOT / 'tests/fixtures/telegram_emergency_grouped.html').read_text(encoding='utf-8')


def message(chapter=3227, *, href=None, title='Private Title'):
    href = href or f'https://t.me/shadow_slave_fastes/{chapter}?single'
    return (f'<div class="tgme_widget_message" data-post="shadow_slave_fastes/{chapter}">'
            f'<a href="{href}"><span class="tgme_widget_message_document_title">'
            f'{chapter} {title}.docx</span></a></div>')


def response(html=OFFICIAL, status=200, *, url=WEBNOVEL_SOURCE.url):
    result = Mock(spec=requests.Response)
    result.status_code, result.url = status, url
    result.headers = {'Content-Type': 'text/html'}
    result.encoding, result.is_redirect = 'utf-8', False
    result.iter_content.return_value = [html.encode()]
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(PRIVATE, response=result)
    return result


class RunnerDiagnosticTests(unittest.TestCase):
    def setUp(self):
        # Both requests-level and socket-level guards make accidental live I/O fail.
        self.network = self.enterContext(patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected network')))
        self.sockets = self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('unexpected socket')))
        self.connections = self.enterContext(patch('socket.create_connection', side_effect=AssertionError('unexpected connection')))
        self.get = self.enterContext(patch('requests.Session.get', side_effect=self.respond))
        self.sleep = self.enterContext(patch('shadow_slave_monitor.http_client.time.sleep'))
        self.saved = self.enterContext(patch.object(state_manager, 'save_state', side_effect=AssertionError('state write')))
        self.atomic = self.enterContext(patch.object(state_manager, 'atomic_write_json', side_effect=AssertionError('state write')))
        self.sent = self.enterContext(patch('shadow_slave_monitor.notifications.send_new_chapter', side_effect=AssertionError('notification')))
        self.tmp = self.enterContext(TemporaryDirectory())
        self.path = Path(self.tmp) / 'snapshot.json'
        self.state = state_manager.initial_state()
        self.state.update(latest_seen=3226, latest_webnovel=3226)
        self.write_state()

    def tearDown(self):
        for blocked in (self.network, self.sockets, self.connections, self.saved, self.atomic, self.sent):
            blocked.assert_not_called()

    def write_state(self):
        self.path.write_text(json.dumps(self.state), encoding='utf-8')

    @staticmethod
    def respond(url, **kwargs):
        if url == WEBNOVEL_SOURCE.url:
            return response()
        if url == SITE.url:
            return response(message(), url=SITE.url)
        raise AssertionError('unexpected URL')

    def run_diagnostic(self):
        before = self.path.read_bytes()
        outcome = diagnostic.diagnose_runner(state_path=self.path)
        self.assertEqual(self.path.read_bytes(), before)
        return outcome

    def assert_safe(self, value):
        output = json.dumps(value)
        for forbidden in ('private-secret', 'untrusted.example', 'token=', 'Private Title', '<html', '<div', 'Traceback', 'Authorization', 'Cookie'):
            self.assertNotIn(forbidden, output)

    def test_both_successes_use_production_checks_and_identical_http_policy(self):
        with patch.object(parsers, 'check_webnovel', wraps=parsers.check_webnovel) as official, \
             patch.object(parsers, 'check_telegram_emergency', wraps=parsers.check_telegram_emergency) as emergency, \
             patch.object(parsers, 'check_public_site', side_effect=AssertionError('ordinary parser')), \
             patch.object(parsers, 'parse_telegram_candidates', side_effect=AssertionError('ordinary parser')):
            outcome = self.run_diagnostic()
        official.assert_called_once_with(WEBNOVEL_SOURCE)
        emergency.assert_called_once_with(SITE, 3226)
        self.assertEqual(self.get.call_count, 2)
        for source, actual in zip((WEBNOVEL_SOURCE, SITE), self.get.call_args_list):
            self.assertEqual(actual, call(source.url, headers=HEADERS, timeout=(5, 20), stream=True, allow_redirects=False))
        for key in ('webnovel', 'telegram_emergency'):
            result = outcome[key]
            self.assertEqual((result['category'], result['http_status'], result['http_completed'], result['chapter_validated'], result['chapter']),
                             ('SUCCESS_VALID_CHAPTER', 200, True, True, 3227))
        self.assertTrue(outcome['telegram_emergency']['newer_than_baseline'])
        self.assert_safe(outcome)

    def test_http_errors_preserve_exact_status_and_bounded_retries_for_both_sources(self):
        for source in (WEBNOVEL_SOURCE, SITE):
            key = 'webnovel' if source == WEBNOVEL_SOURCE else 'telegram_emergency'
            for status in (401, 403, 404, 418, 429, 500, 502, 503, 504):
                with self.subTest(source=key, status=status):
                    self.get.reset_mock()
                    self.get.side_effect = lambda url, **kw: response(status=status, url=url) if url == source.url else self.respond(url, **kw)
                    outcome = self.run_diagnostic()
                    failed = outcome[key]
                    self.assertEqual((failed['category'], failed['http_status'], failed['http_completed'], failed['chapter_validated']),
                                     ('HTTP_ERROR', status, False, False))
                    self.assertTrue(outcome['telegram_emergency' if key == 'webnovel' else 'webnovel']['chapter_validated'])
                    attempts = 3 if status in (429, 500, 502, 503, 504) else 1
                    self.assertEqual(self.get.call_count, attempts + 1)
                    self.assert_safe(outcome)

    def test_network_tls_and_internal_errors_are_independent_and_sanitized(self):
        for key, target in (('webnovel', WEBNOVEL_SOURCE), ('telegram_emergency', SITE)):
            for error, category, classification, attempts in (
                (requests.Timeout, 'NETWORK_ERROR', 'timeout', 3),
                (requests.ConnectionError, 'NETWORK_ERROR', 'connection_error', 3),
                (requests.exceptions.SSLError, 'TLS_ERROR', 'tls_certificate_or_handshake_error', 3),
                (requests.RequestException, 'NETWORK_ERROR', 'request_error', 1),
                (RuntimeError, 'INTERNAL_ERROR', 'unexpected_internal_error', 1),
            ):
                with self.subTest(source=key, error=error):
                    def fail(url, **kwargs):
                        if url == target.url:
                            raise error(PRIVATE)
                        return self.respond(url, **kwargs)
                    self.get.reset_mock()
                    self.get.side_effect = fail
                    outcome = self.run_diagnostic()
                    result = outcome[key]
                    self.assertEqual((result['category'], result['classification'], result['http_status']), (category, classification, 'unavailable'))
                    self.assertFalse(result['http_completed'])
                    self.assertFalse(result['chapter_validated'])
                    self.assertTrue(outcome['telegram_emergency' if key == 'webnovel' else 'webnovel']['chapter_validated'])
                    self.assertEqual(self.get.call_count, attempts + 1)
                    self.assert_safe(outcome)

    def test_webnovel_http_200_requires_trusted_parser_result(self):
        for html in ('', '<html><broken', '<html>No chapter information</html>',
                     OFFICIAL.replace('3227', '10001'), OFFICIAL.replace('3227', '0')):
            with self.subTest(html=html):
                self.get.side_effect = lambda url, **kw: response(html) if url == WEBNOVEL_SOURCE.url else self.respond(url, **kw)
                outcome = self.run_diagnostic()
                result = outcome['webnovel']
                self.assertEqual((result['category'], result['http_status'], result['http_completed'], result['chapter_validated']),
                                 ('PARSE_ERROR', 200, True, False))
                self.assertTrue(outcome['telegram_emergency']['chapter_validated'])

    def test_webnovel_report_number_and_identity_are_revalidated(self):
        for report in (None, ChapterReport('Other', 3227, PRIVATE, PRIVATE),
                       *(ChapterReport('WebNovel', value, PRIVATE, PRIVATE) for value in (0, 10001, True, '3227'))):
            with self.subTest(report=report), patch.object(parsers, 'check_webnovel', return_value=report):
                result = self.run_diagnostic()['webnovel']
                self.assertEqual(result['category'], 'PARSE_ERROR')
                self.assertFalse(result['chapter_validated'])
                self.assert_safe(result)

    def test_grouped_fixture_and_exact_single_document_evidence(self):
        soup = BeautifulSoup(GROUPED, 'html.parser')
        grouped = soup.select_one('[data-post="shadow_slave_fastes/2331"]')
        grouped.select_one('.tgme_widget_message_text').decompose()
        for html, baseline, expected in ((GROUPED, 3226, 3227), (str(grouped), 3216, 3218), (message(), 3226, 3227)):
            with self.subTest(baseline=baseline):
                self.state.update(latest_seen=baseline, latest_webnovel=baseline)
                self.write_state()
                self.get.side_effect = lambda url, **kw: response(html, url=url) if url == SITE.url else self.respond(url, **kw)
                result = self.run_diagnostic()['telegram_emergency']
                self.assertEqual((result['category'], result['chapter']), ('SUCCESS_VALID_CHAPTER', expected))

    def test_equal_older_and_newer_chapters_are_successful_validation(self):
        for chapter, category, newer in ((3225, 'SUCCESS_NO_ADVANCE', False), (3226, 'SUCCESS_NO_ADVANCE', False),
                                         (3227, 'SUCCESS_VALID_CHAPTER', True)):
            with self.subTest(chapter=chapter):
                self.get.side_effect = lambda url, **kw: response(message(chapter), url=url) if url == SITE.url else self.respond(url, **kw)
                result = self.run_diagnostic()['telegram_emergency']
                self.assertEqual((result['category'], result['chapter'], result['baseline'], result['newer_than_baseline']),
                                 (category, chapter, 3226, newer))
                self.assertTrue(result['chapter_validated'])

    def test_pending_chapter_is_part_of_baseline_without_using_authority_target(self):
        self.state['latest_webnovel'] = 3228
        self.state['pending_notification'] = pending_from_report(3226, ChapterReport('Telegram', 3228, 'Pending', SITE.url))
        self.write_state()
        with patch.object(parsers, 'check_telegram_emergency', wraps=parsers.check_telegram_emergency) as check:
            outcome = self.run_diagnostic()
        check.assert_called_once_with(SITE, 3228)
        self.assertEqual(outcome['telegram_emergency']['category'], 'SUCCESS_NO_ADVANCE')
        # The authority's newly returned chapter is not passed to discovery.
        self.assertEqual(outcome['webnovel']['chapter'], 3227)

    def test_invalid_unsafe_missing_contradictory_and_suspicious_evidence(self):
        contradictory = (message() + message(title='Contradictory Title'))
        disagreement = message() + '<div class="tgme_widget_message" data-post="shadow_slave_fastes/9000"><a href="https://telegra.ph/3227-Private-Title">Chapter 3228: Private Title</a></div>'
        cases = [('', 'PARSE_NO_CHAPTER_LINKS'), ('<html><broken', 'PARSE_NO_CHAPTER_LINKS'),
                 ('<div>No evidence</div>', 'PARSE_NO_CHAPTER_LINKS'),
                 (message(10001), 'PARSE_CHAPTER_INVALID'),
                 (contradictory, 'PARSE_AMBIGUOUS_CHAPTER'), (disagreement, 'PARSE_AMBIGUOUS_CHAPTER'),
                 (message(3300), 'PARSE_AMBIGUOUS_CHAPTER')]
        for href in ('https://untrusted.example/3227-private', 'http://t.me/shadow_slave_fastes/3227',
                     'https://t.me/other_channel/3227?single', SITE.url + '/3227?single=1',
                     SITE.url + '/3227?single&x=1', SITE.url + '/3227?%73ingle', SITE.url + '/3227#private'):
            cases.append((message(href=href), 'PARSE_CHAPTER_INVALID'))
        for html, code in cases:
            with self.subTest(code=code, html=html):
                self.get.side_effect = lambda url, **kw: response(html, url=url) if url == SITE.url else self.respond(url, **kw)
                result = self.run_diagnostic()['telegram_emergency']
                self.assertEqual((result['category'], result['classification'], result['http_status'], result['http_completed'], result['chapter_validated']),
                                 ('PARSE_ERROR', code, 200, True, False))
                self.assert_safe(result)

    def test_emergency_report_provenance_is_revalidated(self):
        for report in (None, ChapterReport('Other', 3227, PRIVATE, SITE.url + '/3227'),
                       ChapterReport('Telegram', 3227, PRIVATE, 'https://untrusted.example/private'),
                       ChapterReport('Telegram', True, PRIVATE, SITE.url + '/3227')):
            with self.subTest(report=report), patch.object(parsers, 'check_telegram_emergency', return_value=report):
                result = self.run_diagnostic()['telegram_emergency']
                self.assertEqual(result['category'], 'PARSE_ERROR')
                self.assertFalse(result['chapter_validated'])
                self.assert_safe(result)

    def test_http_policy_errors_preserve_status_without_leaking_headers_or_redirect(self):
        for target in (WEBNOVEL_SOURCE, SITE):
            key = 'webnovel' if target == WEBNOVEL_SOURCE else 'telegram_emergency'
            for policy in ('content_type', 'unsafe_redirect', 'missing_redirect', 'response_size'):
                with self.subTest(source=key, policy=policy):
                    bad = response(PRIVATE, url=target.url)
                    if policy == 'content_type':
                        bad.headers['Content-Type'] = 'application/json; token=secret'
                    elif policy in ('unsafe_redirect', 'missing_redirect'):
                        bad.status_code, bad.is_redirect = 302, True
                        bad.headers = {'Location': 'https://untrusted.example/private?token=secret'} if policy == 'unsafe_redirect' else {}
                    self.get.side_effect = lambda url, **kw: bad if url == target.url else self.respond(url, **kw)
                    with patch('shadow_slave_monitor.http_client.MAX_HTML_BYTES', 1 if policy == 'response_size' else 5 * 1024 * 1024):
                        result = self.run_diagnostic()[key]
                    self.assertEqual(result['category'], 'HTTP_POLICY_ERROR')
                    self.assertEqual(result['http_status'], bad.status_code)
                    self.assertFalse(result['http_completed'])
                    self.assert_safe(result)

    def test_streaming_failure_with_status_200_is_not_completed_retrieval(self):
        bad = response()
        bad.iter_content.side_effect = requests.ConnectionError(PRIVATE)
        self.get.side_effect = lambda url, **kw: bad if url == WEBNOVEL_SOURCE.url else self.respond(url, **kw)
        result = self.run_diagnostic()['webnovel']
        self.assertEqual((result['category'], result['http_status'], result['http_completed']), ('NETWORK_ERROR', 200, False))

    def test_http_error_without_status_explicitly_reports_unavailable(self):
        self.get.side_effect = requests.HTTPError(PRIVATE)
        outcome = self.run_diagnostic()
        for key in ('webnovel', 'telegram_emergency'):
            self.assertEqual(outcome[key]['category'], 'HTTP_ERROR')
            self.assertEqual(outcome[key]['http_status'], 'unavailable')
        self.assert_safe(outcome)

    def test_missing_invalid_and_uninitialized_snapshots_fail_setup_but_authority_runs(self):
        for value in (None, '', '{broken', '[]', '{"latest_seen":10001}', '{}', '{"latest_seen":true}',
                      '{"latest_seen":3226,"unexpected":"' + PRIVATE + '"}'):
            with self.subTest(value=value):
                self.get.reset_mock()
                if value is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.path.write_text(value, encoding='utf-8')
                before = self.path.read_bytes() if self.path.exists() else None
                outcome = diagnostic.diagnose_runner(state_path=self.path)
                self.assertEqual(outcome['telegram_emergency']['category'], 'SETUP_ERROR')
                self.assertEqual(outcome['state_snapshot_sha256'], 'unavailable')
                self.assertTrue(outcome['webnovel']['chapter_validated'])
                self.assertEqual(self.get.call_count, 1)
                self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)
                self.assert_safe(outcome)

    def test_state_is_read_once_and_both_production_state_files_are_unchanged(self):
        files = [ROOT / 'state/state.json', ROOT / 'state/watchdog_state.json']
        before = [path.read_bytes() for path in files]
        with patch.object(Path, 'read_bytes', autospec=True, return_value=self.path.read_bytes()) as read:
            outcome = diagnostic.diagnose_runner(state_path=self.path)
        read.assert_called_once_with(self.path)
        self.assertRegex(outcome['state_snapshot_sha256'], r'^[0-9a-f]{64}$')
        self.assertEqual([path.read_bytes() for path in files], before)

    def test_observer_and_logging_are_restored_on_success_and_internal_failure(self):
        original, level = parsers.fetch_html, logging.root.manager.disable
        for side_effect in (self.respond, RuntimeError(PRIVATE)):
            self.get.side_effect = side_effect
            self.run_diagnostic()
            self.assertIs(parsers.fetch_html, original)
            self.assertEqual(logging.root.manager.disable, level)

    def test_console_and_actions_summary_are_sanitized_and_failures_are_visible(self):
        summary_path = Path(self.tmp) / 'summary.md'
        for source_errors, expected_exit in ((None, 0), (requests.HTTPError(PRIVATE), 0), (RuntimeError(PRIVATE), 2)):
            self.get.side_effect = self.respond if source_errors is None else source_errors
            output = io.StringIO()
            with patch.object(diagnostic, 'diagnose_runner', wraps=lambda: diagnostic_run(state_path=self.path)), \
                 patch.dict(diagnostic.os.environ, {'GITHUB_STEP_SUMMARY': str(summary_path)}), \
                 redirect_stdout(output), redirect_stderr(output):
                self.assertEqual(diagnostic.main(), expected_exit)
            text = output.getvalue()
            self.assert_safe(text)
            self.assert_safe(summary_path.read_text(encoding='utf-8'))
            self.assertIn('Both source checks validated' if source_errors is None else 'SOURCE CHECK FAILURES', text)
            if isinstance(source_errors, requests.HTTPError):
                self.assertIn('::warning::Source check failures', text)

    def test_setup_failure_and_unexpected_summary_failure_exit_nonzero_without_traceback(self):
        self.path.write_text('{}', encoding='utf-8')
        for summary_path in (str(Path(self.tmp) / 'summary.md'), self.tmp):
            output = io.StringIO()
            with patch.object(diagnostic, 'diagnose_runner', wraps=lambda: diagnostic_run(state_path=self.path)), \
                 patch.dict(diagnostic.os.environ, {'GITHUB_STEP_SUMMARY': summary_path}), redirect_stdout(output):
                self.assertEqual(diagnostic.main(), 2)
            self.assert_safe(output.getvalue())

    def test_metadata_is_bounded_and_runner_comparison_has_equivalent_behavior(self):
        results = []
        for label in ('ubuntu-24.04', 'ubuntu-26.04'):
            with patch.dict(diagnostic.os.environ, {'DIAGNOSTIC_RUNNER_LABEL': label, 'ImageOS': 'ubuntu26',
                            'ImageVersion': '20261025.1.0', 'DIAGNOSTIC_CHECKOUT_SHA': 'a' * 40}), \
                 patch('platform.freedesktop_os_release', return_value={'ID': 'ubuntu', 'VERSION_ID': label.removeprefix('ubuntu-')}):
                outcome = self.run_diagnostic()
            self.assertEqual(outcome['runner']['requested_runner'], label)
            self.assertEqual(outcome['runner']['os_version'], label.removeprefix('ubuntu-'))
            for key in ('python', 'requests', 'urllib3', 'openssl', 'executed_at_utc', 'image_version', 'checkout_sha'):
                self.assertNotEqual(outcome['runner'][key], 'unavailable')
            results.append(outcome)
        for key in ('webnovel', 'telegram_emergency', 'state_snapshot_sha256'):
            self.assertEqual(results[0][key], results[1][key])
        with patch.dict(diagnostic.os.environ, {key: PRIVATE for key in ('DIAGNOSTIC_RUNNER_LABEL', 'DIAGNOSTIC_CHECKOUT_SHA', 'ImageOS', 'ImageVersion')}):
            self.assert_safe(diagnostic.runner_information())


# Keep the real entry point available when testing main's output handling.
diagnostic_run = diagnostic.diagnose_runner


class RunnerWorkflowTests(unittest.TestCase):
    def test_manual_matrix_is_read_only_pinned_and_isolated(self):
        workflow = (ROOT / '.github/workflows/runner-compatibility-diagnostics.yml').read_text(encoding='utf-8')
        self.assertIn('on:\n  workflow_dispatch:\n', workflow)
        self.assertIn('runner: [ubuntu-24.04, ubuntu-26.04]', workflow)
        self.assertIn('runs-on: ${{ matrix.runner }}', workflow)
        self.assertIn('fail-fast: false', workflow)
        self.assertIn('permissions:\n  contents: read', workflow)
        self.assertIn('ref: ${{ github.sha }}', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('python-version: "3.12"', workflow)
        self.assertIn('timeout-minutes: 15', workflow)
        self.assertIn('--require-hashes -r dependencies/requirements.lock', workflow)
        self.assertEqual(workflow.count('run: python -m shadow_slave_monitor.diagnose_runner'), 1)
        existing = (ROOT / '.github/workflows/source-diagnostics.yml').read_text(encoding='utf-8')
        for line in workflow.splitlines():
            if 'uses:' in line:
                self.assertIn(line.strip(), existing)
        for forbidden in ('schedule:', 'push:', 'pull_request:', 'workflow_run:', 'ubuntu-latest', 'secrets.',
                          'contents: write', 'concurrency:', 'NTFY', 'upload-artifact', 'shadow_slave_monitor.monitor',
                          'shadow_slave_monitor.watchdog', 'continue-on-error', 'git fetch', 'git pull'):
            self.assertNotIn(forbidden, workflow)
        self.assertIn('runs-on: ubuntu-24.04', (ROOT / '.github/workflows/monitor.yml').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
