from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from shadow_slave_monitor import watchdog


class WatchdogEvaluateTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
