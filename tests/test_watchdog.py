from __future__ import annotations

import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

from shadow_slave_monitor import watchdog


class WatchdogEvaluateTests(unittest.TestCase):
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
