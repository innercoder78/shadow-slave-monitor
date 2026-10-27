"""Offline checks of the scheduled workflow and its existing CLI execution path."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import requests

from shadow_slave_monitor import diagnose_runner as diagnostic, state_manager
from shadow_slave_monitor.config import PUBLIC_SITES
from shadow_slave_monitor.diagnostics import HtmlDocument, ResponseMetadata


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/scheduled-public-diagnostics.yml'
NAMES = ('Novel Phoenix', 'NovelPing')
ENABLED = ('Chikari', 'Telegram', 'Novel Buddy', 'ShadowSlave.Space', 'FreeWebNovel',
           'SSNovel', 'NovelFull', 'LightNovelUp', 'ReadNovelFull', 'FreeWebNovel.net', 'Rentry')


def document(site, *, bad_host=False):
    path = ('/novel/shadow-slave/chapter-3227' if site.name == NAMES[0]
            else '/book/shadow-slave/chapter-3227-a-new-dawn')
    marker = 'Latest Release:' if site.name == NAMES[0] else 'Latest chapter:'
    href = 'https://untrusted.example' + path if bad_host else path
    return HtmlDocument(f'<h1>Shadow Slave</h1><section><b>{marker}</b>'
                        f'<a href="{href}">Chapter 3227 A New Dawn</a></section>',
                        ResponseMetadata(200, site.allowed_hosts[0], 1))


class ScheduledWorkflowTests(unittest.TestCase):
    def test_triggers_and_only_optional_reference_input(self):
        workflow = WORKFLOW.read_text()
        triggers = workflow.split('\npermissions:', 1)[0]
        self.assertIn("- cron: '17 3,15 * * *'", triggers)
        self.assertEqual(triggers.count('cron:'), 1)
        self.assertIn('  workflow_dispatch:\n    inputs:\n      reference_chapter:', triggers)
        self.assertIn('type: string\n        required: false\n        default: ""', triggers)
        self.assertEqual(re.findall(r'^      (\w+):$', triggers, re.M), ['reference_chapter'])
        self.assertNotIn('workflow_run:', workflow)

    def test_read_only_independent_job_and_pinned_setup(self):
        workflow = WORKFLOW.read_text()
        self.assertIn('name: Scheduled public source diagnostics', workflow)
        self.assertIn('permissions:\n  contents: read\n', workflow)
        self.assertEqual(re.findall(r'^    runs-on: (.+)$', workflow, re.M), ['ubuntu-26.04'])
        self.assertIn('timeout-minutes: 15', workflow)
        self.assertIn('python-version: "3.12"', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('ref: ${{ github.sha }}', workflow)
        manual = (ROOT / '.github/workflows/runner-compatibility-diagnostics.yml').read_text()
        actions = re.findall(r'uses: (\S+)', workflow)
        self.assertEqual(len(actions), 2)
        for action in actions:
            self.assertRegex(action, r'@\w{40}$')
            self.assertIn('uses: ' + action, manual)
        for forbidden in ('secrets.', ': write', 'needs:', 'concurrency:', 'state_writer',
                          'shadow_slave_monitor.monitor', 'upload-artifact', 'download-artifact',
                          'api.github.com', 'continue-on-error', '|| true'):
            self.assertNotIn(forbidden, workflow)
        self.assertEqual(re.findall(r'^        run: (.+)$', workflow, re.M), [
            'python -m pip install --require-hashes -r dependencies/requirements.lock',
            'python -m shadow_slave_monitor.diagnose_runner'])

    def test_fixed_inactive_mode_and_no_scheduled_reference(self):
        workflow = WORKFLOW.read_text()
        self.assertIn('DIAGNOSTIC_MODE: inactive_sources', workflow)
        self.assertIn('DIAGNOSTIC_RUNNER_LABEL: ubuntu-26.04', workflow)
        self.assertIn('DIAGNOSTIC_CHECKOUT_SHA: ${{ github.sha }}', workflow)
        self.assertIn("DIAGNOSTIC_REFERENCE_CHAPTER: ${{ github.event_name == 'workflow_dispatch' && inputs.reference_chapter || '' }}", workflow)
        for forbidden in ('core', 'diagnostic_mode:', 'source_name:', 'url:', 'WEBNOVEL', 'TELEGRAM'):
            self.assertNotIn(forbidden, workflow)

    def test_disabled_registry_and_existing_order(self):
        self.assertEqual(diagnostic.INACTIVE_SOURCE_NAMES, NAMES)
        self.assertEqual(tuple(s.name for s in PUBLIC_SITES if s.enabled), ENABLED)
        self.assertEqual(tuple(s.name for s in PUBLIC_SITES if not s.enabled), NAMES)
        self.assertTrue(all(diagnostic._inactive_source(name).enabled is False for name in NAMES))

    def test_abandoned_files_and_production_hooks_are_absent(self):
        for relative in ('.github/workflows/isolated-public-worker.yml',
                         'src/shadow_slave_monitor/isolated_worker.py',
                         'src/shadow_slave_monitor/isolated_handoff.py',
                         'src/shadow_slave_monitor/isolated_evidence.py',
                         'docs/isolated-public-runner.md', 'tests/test_isolated_public_runner.py'):
            self.assertFalse((ROOT / relative).exists(), relative)
        for relative in ('.github/workflows/monitor.yml', 'scripts/state_writer.py',
                         'src/shadow_slave_monitor/monitor.py', 'src/shadow_slave_monitor/config.py',
                         'src/shadow_slave_monitor/state_manager.py'):
            text = (ROOT / relative).read_text()
            for hook in ('isolated_handoff', 'isolated_public_source_failures',
                         'SHADOW_SLAVE_ISOLATED', 'ISOLATED_PUBLIC_SOURCE_NAMES'):
                self.assertNotIn(hook, text, relative)
        self.assertNotIn('max_attempts', (ROOT / 'src/shadow_slave_monitor/http_client.py').read_text())


class ScheduledExecutionTests(unittest.TestCase):
    def setUp(self):
        self.blocked = []
        for target in ('requests.sessions.Session.request', 'socket.socket.connect',
                       'socket.getaddrinfo', 'shadow_slave_monitor.notifications._post_ntfy'):
            self.blocked.append(self.enterContext(patch(target, side_effect=AssertionError('forbidden I/O'))))
        for name in ('load_state', 'save_state', 'atomic_write_json'):
            self.blocked.append(self.enterContext(patch.object(state_manager, name,
                                                              side_effect=AssertionError('state access'))))
        for name in ('diagnose_runner', '_baseline_snapshot', 'diagnose_webnovel', 'diagnose_telegram'):
            self.blocked.append(self.enterContext(patch.object(diagnostic, name,
                                                              side_effect=AssertionError('core mode'))))
        self.temp = self.enterContext(TemporaryDirectory())
        self.summary = Path(self.temp) / 'summary.md'
        self.enterContext(patch.dict(os.environ, {
            'GITHUB_EVENT_NAME': 'schedule', 'GITHUB_SHA': 'a' * 40,
            'DIAGNOSTIC_MODE': 'inactive_sources', 'DIAGNOSTIC_REFERENCE_CHAPTER': '',
            'DIAGNOSTIC_RUNNER_LABEL': 'ubuntu-26.04', 'DIAGNOSTIC_CHECKOUT_SHA': 'a' * 40,
            'GITHUB_STEP_SUMMARY': str(self.summary), 'ImageOS': 'ubuntu26',
        }))
        self.enterContext(patch.object(diagnostic.platform, 'freedesktop_os_release',
                                      return_value={'ID': 'ubuntu', 'VERSION_ID': '26.04'}))
        self.fetch = self.enterContext(patch.object(diagnostic.http_client, 'fetch_html', side_effect=document))
        self.before = {p: p.read_bytes() for p in (ROOT / 'state').glob('*.json')}

    def tearDown(self):
        for blocked in self.blocked:
            blocked.assert_not_called()
        for path, content in self.before.items():
            self.assertEqual(path.read_bytes(), content)

    def run_cli(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = diagnostic.main()
        payload = None
        if self.summary.exists():
            payload = json.loads(self.summary.read_text().split('```json\n')[-1].split('\n```')[0])
        return code, payload, output.getvalue()

    def test_schedule_records_two_trusted_candidates_and_run_context(self):
        code, result, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual([call.args[0].name for call in self.fetch.call_args_list], list(NAMES))
        self.assertEqual([s['chapter_candidate'] for s in result['sources']], [3227, 3227])
        self.assertTrue(all(not s['inconclusive'] for s in result['sources']))
        self.assertTrue(all(s['reference_chapter'] is None and s['jump_check'] == 'not_checked_without_reference'
                            for s in result['sources']))
        runner = result['runner']
        self.assertEqual(runner['trigger'], 'schedule')
        self.assertEqual(runner['checkout_sha'], 'a' * 40)
        self.assertEqual(runner['requested_runner'], 'ubuntu-26.04')
        self.assertTrue(runner['executed_at_utc'].endswith('+00:00'))

    def test_manual_reference_is_used_and_trigger_identified(self):
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'workflow_dispatch',
                                     'DIAGNOSTIC_REFERENCE_CHAPTER': '3226'}):
            code, result, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(result['runner']['trigger'], 'workflow_dispatch')
        self.assertTrue(all(s['reference_chapter'] == 3226 and s['jump_check'] == 'within_limit'
                            for s in result['sources']))

    def test_invalid_manual_reference_fails_before_any_fetch(self):
        for value in ('0', '10001', '-1', ' 3227', '3.2', '3227;echo secret'):
            with self.subTest(value=value), patch.dict(os.environ, {
                    'GITHUB_EVENT_NAME': 'workflow_dispatch', 'DIAGNOSTIC_REFERENCE_CHAPTER': value}):
                self.assertEqual(self.run_cli()[0], 2)
        self.fetch.assert_not_called()

    def test_expected_network_failure_retains_successful_peer_in_both_orders(self):
        for failed in NAMES:
            def fetch(site):
                if site.name == failed:
                    raise requests.Timeout('private-sensitive-exception')
                return document(site)
            self.fetch.reset_mock(side_effect=True)
            self.fetch.side_effect = fetch
            code, result, output = self.run_cli()
            self.assertEqual(code, 0)
            self.assertEqual(self.fetch.call_count, 2)
            entries = {s['source']: s for s in result['sources']}
            self.assertEqual(entries[failed]['classification'], 'timeout')
            self.assertIsNone(entries[failed]['chapter_candidate'])
            self.assertEqual(entries[next(n for n in NAMES if n != failed)]['chapter_candidate'], 3227)
            self.assertNotIn('private-sensitive-exception', output)

    def test_wrong_host_is_inconclusive_despite_http_success(self):
        self.fetch.side_effect = lambda site: document(site, bad_host=True)
        code, result, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertTrue(all(s['http_completed'] and s['chapter_candidate'] is None
                            and s['inconclusive'] for s in result['sources']))

    def test_internal_failure_fails_run_but_peer_still_checked(self):
        def fetch(site):
            if site.name == NAMES[0]:
                raise RuntimeError('private')
            return document(site)
        self.fetch.side_effect = fetch
        code, result, output = self.run_cli()
        self.assertEqual(code, 2)
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(result['sources'][0]['category'], 'INTERNAL_ERROR')
        self.assertEqual(result['sources'][1]['chapter_candidate'], 3227)
        self.assertNotIn('private', output)

    def test_summary_infrastructure_failure_is_nonzero(self):
        with patch.object(Path, 'open', side_effect=OSError('private')):
            code, _, output = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn('Diagnostic infrastructure failure', output)
        self.assertNotIn('private', output)

    def test_untrusted_event_metadata_is_not_echoed(self):
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'private-untrusted'}):
            self.assertEqual(diagnostic.runner_information()['trigger'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
