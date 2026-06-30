"""Unit tests for scripts/send_health_report.py (daily enrichment email).

Covers message building from the summary JSON (success + failure shapes) and the
SMTP send path with a mocked smtplib so no real email is sent.

Run with:  python -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import send_health_report as shr  # noqa: E402


def _summary(ok=True):
    return {
        "date": "2026-06-30",
        "providers_used": ["serper"],
        "companies_processed": 50,
        "serper_searches_used": 50,
        "accepted": 20,
        "written": 20,
        "rejected": 30,
        "rejected_by_reason": [
            ["no snippet matched registry_number/court (strict gate)", 18],
            ["empty-meaning activity (not stored)", 2],
        ],
        "high": 8,
        "medium": 12,
        "total_enriched_now": 1179,
        "eligible_total": 2900,
        "coverage_percent": 40.7,
        "ok": ok,
        "error": "" if ok else "boom",
        "dry_run": False,
    }


class TestBuildReportMessage(unittest.TestCase):
    def test_success_message_has_subject_recipient_and_stats(self):
        m = shr.build_report_message(
            _summary(), to_addr="himmat.aljasem@swift-assets.de",
            from_addr="bot@swift-assets.de")
        self.assertEqual(m["To"], "himmat.aljasem@swift-assets.de")
        self.assertEqual(m["From"], "bot@swift-assets.de")
        self.assertIn("Daily Enrichment Health Check", m["Subject"])
        self.assertIn("2026-06-30", m["Subject"])
        body = m.get_content()
        self.assertIn("50", body)        # processed / serper searches
        self.assertIn("40.7", body)      # coverage percent
        self.assertIn("serper", body)    # provider used
        self.assertIn("strict gate", body)  # top reject reason

    def test_failure_subject_when_ok_false(self):
        m = shr.build_report_message(
            _summary(ok=False), to_addr="a@b", from_addr="c@d")
        self.assertIn("فشل", m["Subject"])
        self.assertIn("boom", m.get_content())

    def test_missing_summary_produces_failure_report(self):
        m = shr.build_report_message(
            None, to_addr="a@b", from_addr="c@d", date_fallback="2026-06-30")
        self.assertEqual(m["To"], "a@b")
        self.assertIn("فشل", m["Subject"])
        self.assertIn("2026-06-30", m["Subject"])
        self.assertIn("summary", m.get_content().lower())


class TestSendMessage(unittest.TestCase):
    def _fake_smtp(self, calls):
        class FakeSMTP:
            def __init__(self, host, port, timeout=60):
                calls["host"] = host
                calls["port"] = port

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def ehlo(self):
                calls["ehlo"] = True

            def starttls(self):
                calls["starttls"] = True

            def login(self, user, password):
                calls["login"] = (user, password)

            def send_message(self, msg):
                calls["sent_subject"] = msg["Subject"]

        return FakeSMTP

    def test_starttls_path_on_port_587(self):
        calls = {}
        orig = shr.smtplib.SMTP
        shr.smtplib.SMTP = self._fake_smtp(calls)
        try:
            m = shr.build_report_message(_summary(), to_addr="a@b", from_addr="c@d")
            shr.send_message(m, host="smtp.example.com", port=587,
                             user="u", password="p")
        finally:
            shr.smtplib.SMTP = orig
        self.assertTrue(calls.get("starttls"))
        self.assertEqual(calls["login"], ("u", "p"))
        self.assertIn("Daily Enrichment Health Check", calls["sent_subject"])

    def test_implicit_tls_path_on_port_465(self):
        calls = {}
        orig = shr.smtplib.SMTP_SSL
        shr.smtplib.SMTP_SSL = self._fake_smtp(calls)
        try:
            m = shr.build_report_message(_summary(), to_addr="a@b", from_addr="c@d")
            shr.send_message(m, host="smtp.example.com", port=465,
                             user="u", password="p")
        finally:
            shr.smtplib.SMTP_SSL = orig
        self.assertEqual(calls["port"], 465)
        self.assertEqual(calls["login"], ("u", "p"))
        self.assertNotIn("starttls", calls)  # implicit TLS, no STARTTLS


if __name__ == "__main__":
    unittest.main()
