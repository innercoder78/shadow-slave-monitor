from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import requests

from shadow_slave_monitor.config import SourceConfig
from shadow_slave_monitor.http_client import fetch_html, safe_exception_category, safe_exception_details


class HttpDiagnosticsTests(unittest.TestCase):
    source = SourceConfig("Some Source", "https://example.com/private?token=secret", True, ("example.com",))

    @staticmethod
    def response(status: int, url: str = "https://example.com/private?token=secret") -> Mock:
        response = Mock(spec=requests.Response)
        response.status_code = status
        response.url = url
        response.is_redirect = False
        response.headers = {"Content-Type": "text/html"}
        response.encoding = "utf-8"
        response.iter_content.return_value = [b"ok"]
        if status >= 400:
            request = requests.Request("GET", url).prepare()
            response.raise_for_status.side_effect = requests.HTTPError("body=secret", response=response, request=request)
        return response

    def test_http_error_has_only_safe_status_host_and_attempts(self) -> None:
        with patch("requests.Session.get", return_value=self.response(403)):
            with self.assertRaises(requests.HTTPError) as caught:
                fetch_html(self.source)
        details = safe_exception_details(caught.exception)
        self.assertEqual(safe_exception_category(caught.exception), "http_error")
        self.assertEqual(details, "status=403 host=example.com attempts=1")
        self.assertNotIn("private", details)
        self.assertNotIn("secret", details)

    def test_retry_exhaustion_reports_final_status_and_attempts(self) -> None:
        with patch("requests.Session.get", return_value=self.response(503)), patch("time.sleep"):
            with self.assertRaises(requests.HTTPError) as caught:
                fetch_html(self.source)
        self.assertEqual(safe_exception_details(caught.exception), "status=503 host=example.com attempts=3")

    def test_rejected_redirect_has_controlled_reason_and_rejected_host(self) -> None:
        response = self.response(302)
        response.is_redirect = True
        response.headers = {"Location": "https://unexpected.example/path?token=secret"}
        with patch("requests.Session.get", return_value=response):
            with self.assertRaises(Exception) as caught:
                fetch_html(self.source)
        self.assertEqual(safe_exception_category(caught.exception), "http_policy_error")
        self.assertEqual(safe_exception_details(caught.exception),
                         "reason=redirect_host_not_allowed host=unexpected.example attempts=1")

    def test_timeout_reports_total_attempts_without_raw_message(self) -> None:
        request = requests.Request("GET", self.source.url).prepare()
        with patch("requests.Session.get", side_effect=requests.ReadTimeout("authorization=cookie", request=request)), patch("time.sleep"):
            with self.assertRaises(requests.ReadTimeout) as caught:
                fetch_html(self.source)
        details = safe_exception_details(caught.exception)
        self.assertEqual(details, "host=example.com attempts=3")
        self.assertNotIn("authorization", details)
        self.assertNotIn("cookie", details)


if __name__ == "__main__":
    unittest.main()
