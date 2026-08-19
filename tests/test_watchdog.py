from __future__ import annotations

import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests

from shadow_slave_monitor import watchdog


class WatchdogGitHubRequestTests(unittest.TestCase):
    @staticmethod
    def response(status: int = 200, *, json_data: object | None = None, content: bytes = b"") -> Mock:
        response = Mock(spec=requests.Response)
        response.status_code = status
        response.content = content
        response.json.return_value = {} if json_data is None else json_data
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(response=response)
        return response

    @patch.object(watchdog.time, "sleep")
    @patch.object(watchdog.requests, "get")
    def test_connection_error_is_retried_then_json_request_succeeds(self, get: Mock, sleep: Mock) -> None:
        response = self.response(json_data={"workflow_runs": []})
        get.side_effect = [requests.ConnectionError("certificate verification failed"), response]

        self.assertEqual(watchdog.github_get("/example"), {"workflow_runs": []})
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(1.0)

    @patch.object(watchdog.time, "sleep")
    @patch.object(watchdog.requests, "get")
    def test_timeout_is_retried(self, get: Mock, sleep: Mock) -> None:
        get.side_effect = [requests.Timeout("timed out"), self.response(json_data={"ok": True})]

        self.assertEqual(watchdog.github_get("/example"), {"ok": True})
        sleep.assert_called_once_with(1.0)

    @patch.object(watchdog.time, "sleep")
    @patch.object(watchdog.requests, "get")
    def test_persistent_connection_error_propagates_after_three_attempts(self, get: Mock, sleep: Mock) -> None:
        get.side_effect = requests.ConnectionError("still unavailable")

        with self.assertRaises(requests.ConnectionError):
            watchdog.github_get("/example")

        self.assertEqual(get.call_count, 3)
        self.assertEqual([call.args for call in sleep.call_args_list], [(1.0,), (2.0,)])

    @patch.object(watchdog.time, "sleep")
    @patch.object(watchdog.requests, "get")
    def test_temporary_http_status_is_retried_then_succeeds(self, get: Mock, sleep: Mock) -> None:
        temporary = self.response(503)
        success = self.response(json_data={"ok": True})
        get.side_effect = [temporary, success]

        self.assertEqual(watchdog.github_get("/example"), {"ok": True})
        temporary.close.assert_called_once_with()
        sleep.assert_called_once_with(1.0)

    @patch.object(watchdog.time, "sleep")
    @patch.object(watchdog.requests, "get")
    def test_permanent_http_status_is_not_retried(self, get: Mock, sleep: Mock) -> None:
        get.return_value = self.response(404)

        with self.assertRaises(requests.HTTPError):
            watchdog.github_get("/example")

        get.assert_called_once()
        sleep.assert_not_called()

    @patch.object(watchdog.time, "sleep")
    @patch.object(watchdog.requests, "get")
    def test_artifact_download_uses_shared_retry_behavior(self, get: Mock, sleep: Mock) -> None:
        get.side_effect = [requests.Timeout("timed out"), self.response(content=b"archive")]

        self.assertEqual(watchdog.github_get_bytes("https://api.github.com/artifact"), b"archive")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(1.0)


class WatchdogEvaluateTests(unittest.TestCase):
    @staticmethod
    def state(last_success_at: str, **updates: object) -> dict[str, object]:
        state: dict[str, object] = {
            "current_outage_id": "known-success",
            "open_outage_id": None,
            "last_alert_at": None,
            "last_alert_outage_id": None,
            "last_success_at": last_success_at,
            "latest_failed_conclusion": None,
            "latest_failed_run_url": None,
            "resolved_at": None,
        }
        state.update(updates)
        return state

    @staticmethod
    def monitor_run(identifier: int, timestamp: str, *, result: str = "healthy") -> dict[str, object]:
        return {
            "id": identifier,
            "status": "completed",
            "conclusion": "success",
            "monitor_result": result,
            "created_at": timestamp,
            "updated_at": timestamp,
            "html_url": f"https://github.com/innercoder78/shadow-slave-monitor/actions/runs/{identifier}",
        }

    def test_regressive_successful_history_is_suppressed_without_state_changes(self) -> None:
        state = self.state(
            "2026-06-10T11:00:00Z",
            open_outage_id="known-success",
            latest_failed_conclusion="failure",
        )
        original = state.copy()
        runs = [self.monitor_run(10, "2026-06-10T05:00:00Z")]

        with patch.object(watchdog, "send_watchdog") as send, self.assertLogs(level="WARNING") as logs:
            new_state, changed, status = watchdog.evaluate(runs, state)

        self.assertFalse(changed)
        self.assertEqual(status, "suppressed_regressive_history")
        self.assertEqual(new_state, original)
        send.assert_not_called()
        self.assertIn("regressed behind the persisted known success", " ".join(logs.output))

    def test_no_success_and_all_returned_runs_predate_known_success_is_suppressed(self) -> None:
        state = self.state("2026-06-10T11:00:00Z")
        original = state.copy()
        runs = [self.monitor_run(10, "2026-06-10T05:00:00Z", result="failed")]

        with patch.object(watchdog, "send_watchdog") as send:
            new_state, changed, status = watchdog.evaluate(runs, state)

        self.assertEqual((changed, status), (False, "suppressed_regressive_history"))
        self.assertEqual(new_state, original)
        send.assert_not_called()

    def test_older_success_with_newer_failure_is_still_suppressed_as_incomplete(self) -> None:
        state = self.state("2026-06-10T09:00:00Z")
        original = state.copy()
        runs = [
            self.monitor_run(10, "2026-06-10T05:00:00Z"),
            self.monitor_run(20, "2026-06-10T11:50:00Z", result="failed"),
        ]

        with patch.object(watchdog, "send_watchdog") as send:
            new_state, changed, status = watchdog.evaluate(runs, state)

        self.assertEqual((changed, status), (False, "suppressed_regressive_history"))
        self.assertEqual(new_state, original)
        send.assert_not_called()

    def test_newer_failed_run_without_returned_success_preserves_outage_detection(self) -> None:
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        state = self.state("2026-06-10T06:00:00Z")
        failed = self.monitor_run(20, "2026-06-10T11:50:00Z", result="failed")

        with patch.object(watchdog, "utc_now", return_value=now), patch.object(watchdog, "send_watchdog") as send:
            new_state, changed, status = watchdog.evaluate([failed], state)

        self.assertTrue(changed)
        self.assertEqual(status, "alert_sent")
        self.assertEqual(new_state["last_success_at"], "2026-06-10T06:00:00Z")
        send.assert_called_once()

    def test_success_equal_to_known_success_uses_normal_stale_throttling(self) -> None:
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        state = self.state(
            "2026-06-10T06:00:00Z",
            current_outage_id="10",
            open_outage_id="10",
            last_alert_at="2026-06-10T11:00:00+00:00",
            last_alert_outage_id="10",
        )

        with patch.object(watchdog, "utc_now", return_value=now), patch.object(watchdog, "send_watchdog") as send:
            new_state, changed, status = watchdog.evaluate([self.monitor_run(10, "2026-06-10T06:00:00Z")], state)

        self.assertEqual((changed, status), (False, "throttled"))
        self.assertEqual(new_state, state)
        send.assert_not_called()

    def test_newer_success_advances_state_during_normal_recovery(self) -> None:
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        state = self.state(
            "2026-06-10T06:00:00Z",
            open_outage_id="known-success",
            latest_failed_conclusion="failed",
        )

        with patch.object(watchdog, "utc_now", return_value=now), patch.object(watchdog, "send_watchdog") as send:
            new_state, changed, status = watchdog.evaluate([self.monitor_run(20, "2026-06-10T11:50:00Z")], state)

        self.assertEqual((changed, status), (True, "fresh"))
        self.assertEqual(new_state["last_success_at"], "2026-06-10T11:50:00Z")
        self.assertIsNone(new_state["open_outage_id"])
        send.assert_not_called()

    def test_fresh_success_without_open_outage_or_failure_context_is_noop(self) -> None:
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        success = {
            "id": 12345,
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-06-10T11:49:30Z",
            "updated_at": "2026-06-10T11:50:00Z",
            "html_url": "https://github.com/innercoder78/shadow-slave-monitor/actions/runs/12345",
        }
        state = {
            "current_outage_id": "old-success",
            "open_outage_id": None,
            "last_alert_at": None,
            "last_alert_outage_id": None,
            "last_success_at": "2026-06-10T10:50:00Z",
            "latest_failed_conclusion": None,
            "latest_failed_run_url": None,
            "resolved_at": "2026-06-10T11:00:00+00:00",
        }

        with patch.object(watchdog, "utc_now", return_value=now):
            new_state, changed, status = watchdog.evaluate([success], state.copy())

        self.assertFalse(changed)
        self.assertEqual(status, "fresh")
        self.assertEqual(new_state, state)

    def test_fresh_success_clears_stale_failure_context_and_resolves_open_outage(self) -> None:
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        success = {
            "id": 12345,
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-06-10T10:49:30Z",
            "updated_at": "2026-06-10T10:50:00Z",
            "html_url": "https://github.com/innercoder78/shadow-slave-monitor/actions/runs/12345",
        }
        previous_failure = {
            "id": 12344,
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-06-10T07:49:30Z",
            "updated_at": "2026-06-10T07:50:00Z",
            "html_url": "https://github.com/innercoder78/shadow-slave-monitor/actions/runs/12344",
        }
        state = {
            "current_outage_id": "old-success",
            "open_outage_id": "old-success",
            "last_alert_at": "2026-06-10T08:00:00+00:00",
            "last_alert_outage_id": "old-success",
            "last_success_at": "2026-06-10T04:50:00Z",
            "latest_failed_conclusion": "failure",
            "latest_failed_run_url": previous_failure["html_url"],
            "resolved_at": None,
        }

        with patch.object(watchdog, "utc_now", return_value=now):
            new_state, changed, status = watchdog.evaluate([previous_failure, success], state.copy())

        self.assertTrue(changed)
        self.assertEqual(status, "fresh")
        self.assertEqual(new_state["last_success_at"], success["updated_at"])
        self.assertIsNone(new_state["latest_failed_conclusion"])
        self.assertIsNone(new_state["latest_failed_run_url"])
        self.assertIsNone(new_state["open_outage_id"])
        self.assertEqual(new_state["resolved_at"], "2026-06-10T12:00:00+00:00")
        self.assertEqual(new_state["last_alert_at"], "2026-06-10T08:00:00+00:00")
        self.assertEqual(new_state["last_alert_outage_id"], "old-success")
        self.assertEqual(new_state["current_outage_id"], "12345")

    def test_failed_monitor_health_result_does_not_refresh_success_signal(self) -> None:
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        failed = {
            "id": 12346,
            "status": "completed",
            "conclusion": "success",
            "monitor_result": "failed",
            "created_at": "2026-06-10T11:49:30Z",
            "updated_at": "2026-06-10T11:50:00Z",
            "html_url": "https://github.com/innercoder78/shadow-slave-monitor/actions/runs/12346",
        }
        state = {
            "current_outage_id": "old-success",
            "open_outage_id": None,
            "last_alert_at": None,
            "last_alert_outage_id": None,
            "last_success_at": "2026-06-10T06:00:00Z",
            "latest_failed_conclusion": None,
            "latest_failed_run_url": None,
            "resolved_at": None,
        }

        with patch.object(watchdog, "utc_now", return_value=now), patch.object(watchdog, "send_watchdog") as send:
            new_state, changed, status = watchdog.evaluate([failed], state.copy())

        self.assertTrue(changed)
        self.assertEqual(status, "alert_sent")
        send.assert_called_once()
        self.assertEqual(new_state["last_success_at"], "2026-06-10T06:00:00Z")
        self.assertEqual(new_state["latest_failed_conclusion"], "failed")
        self.assertEqual(new_state["latest_failed_run_url"], failed["html_url"])

    def test_degraded_monitor_health_result_refreshes_success_signal(self) -> None:
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        degraded = {
            "id": 12347,
            "status": "completed",
            "conclusion": "success",
            "monitor_result": "degraded",
            "created_at": "2026-06-10T11:49:30Z",
            "updated_at": "2026-06-10T11:50:00Z",
            "html_url": "https://github.com/innercoder78/shadow-slave-monitor/actions/runs/12347",
        }
        state = {
            "current_outage_id": "old-success",
            "open_outage_id": "old-success",
            "last_alert_at": "2026-06-10T08:00:00+00:00",
            "last_alert_outage_id": "old-success",
            "last_success_at": "2026-06-10T04:50:00Z",
            "latest_failed_conclusion": "failed",
            "latest_failed_run_url": "https://github.com/innercoder78/shadow-slave-monitor/actions/runs/12346",
            "resolved_at": None,
        }

        with patch.object(watchdog, "utc_now", return_value=now):
            new_state, changed, status = watchdog.evaluate([degraded], state.copy())

        self.assertTrue(changed)
        self.assertEqual(status, "fresh")
        self.assertEqual(new_state["last_success_at"], degraded["updated_at"])
        self.assertIsNone(new_state["latest_failed_conclusion"])
        self.assertIsNone(new_state["latest_failed_run_url"])
        self.assertIsNone(new_state["open_outage_id"])

    def test_missing_monitor_health_artifacts_do_not_refresh_stale_success_signal(self) -> None:
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        missing = {
            "id": 12348,
            "status": "completed",
            "conclusion": "success",
            "monitor_result": None,
            "created_at": "2026-06-10T11:49:30Z",
            "updated_at": "2026-06-10T11:50:00Z",
            "html_url": "https://github.com/innercoder78/shadow-slave-monitor/actions/runs/12348",
        }
        state = {
            "current_outage_id": "old-success",
            "open_outage_id": None,
            "last_alert_at": None,
            "last_alert_outage_id": None,
            "last_success_at": "2026-06-10T06:00:00Z",
            "latest_failed_conclusion": None,
            "latest_failed_run_url": None,
            "resolved_at": None,
        }

        with patch.object(watchdog, "utc_now", return_value=now), patch.object(watchdog, "send_watchdog") as send:
            new_state, changed, status = watchdog.evaluate([missing], state.copy())

        self.assertTrue(changed)
        self.assertEqual(status, "alert_sent")
        send.assert_called_once()
        self.assertEqual(new_state["latest_failed_conclusion"], "success")

    def test_stale_monitor_alert_throttling_avoids_repeated_alert_for_unresolved_outage(self) -> None:
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        success = {
            "id": 12345,
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-06-10T05:49:30Z",
            "updated_at": "2026-06-10T05:50:00Z",
            "html_url": "https://github.com/innercoder78/shadow-slave-monitor/actions/runs/12345",
        }
        state = {
            "current_outage_id": "12345",
            "open_outage_id": "12345",
            "last_alert_at": "2026-06-10T11:00:00+00:00",
            "last_alert_outage_id": "12345",
            "last_success_at": "2026-06-10T05:50:00Z",
            "latest_failed_conclusion": "failure",
            "latest_failed_run_url": "https://github.com/innercoder78/shadow-slave-monitor/actions/runs/12346",
            "resolved_at": None,
        }

        with patch.object(watchdog, "utc_now", return_value=now), patch.object(watchdog, "send_watchdog") as send:
            new_state, changed, status = watchdog.evaluate([success], state.copy())

        self.assertFalse(changed)
        self.assertEqual(status, "throttled")
        send.assert_not_called()
        self.assertEqual(new_state, state)


class WatchdogBuildBodyTests(unittest.TestCase):
    def test_build_body_uses_created_at_when_updated_at_is_missing(self) -> None:
        success = {
            "id": 12345,
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-06-10T10:49:30Z",
            "html_url": "https://github.com/innercoder78/shadow-slave-monitor/actions/runs/12345",
        }
        latest = {
            "id": 12346,
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-06-10T11:49:30Z",
            "updated_at": "2026-06-10T11:50:00Z",
            "html_url": "https://github.com/innercoder78/shadow-slave-monitor/actions/runs/12346",
        }

        body = watchdog.build_body(success, latest)

        self.assertIn("Latest successful monitor workflow: 2026-06-10T10:49:30Z", body)
        self.assertNotIn("Latest successful monitor workflow: never", body)


class WatchdogArtifactTests(unittest.TestCase):
    def test_zip_run_result_parser_accepts_healthy_and_rejects_invalid(self) -> None:
        import io
        import json
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("run_result.json", json.dumps({"result": "healthy", "reasons": [], "degraded_reasons": []}))
        self.assertEqual(watchdog.monitor_result_from_zip(buffer.getvalue()), "healthy")

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("run_result.json", json.dumps({"result": "bad", "reasons": [], "degraded_reasons": []}))
        self.assertIsNone(watchdog.monitor_result_from_zip(buffer.getvalue()))


class WorkflowPolicyTests(unittest.TestCase):
    def test_monitor_workflow_does_not_propagate_failed_health_result(self) -> None:
        text = Path(".github/workflows/monitor.yml").read_text(encoding="utf-8")

        self.assertNotIn("Propagate monitor failure result", text)
        self.assertNotIn("Monitor recorded a failed health result", text)


if __name__ == "__main__":
    unittest.main()
