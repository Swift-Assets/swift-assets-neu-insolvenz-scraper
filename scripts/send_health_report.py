#!/usr/bin/env python3
"""
send_health_report.py — email the daily enrichment health-check report.
================================================================================

Reads the summary JSON written by ``scripts/enrich_company_activity.py``
(``SUMMARY_JSON_PATH``, default ``/tmp/enrich_summary.json``) and sends a short
plain-text email (Arabic) with the run's stats. Designed to ALWAYS run after the
worker step in the daily workflow — if the worker failed (or wrote no summary),
the email says so instead of silently skipping.

SMTP is configured from the environment (never hardcoded):
* ``SMTP_HOST``   — required
* ``SMTP_PORT``   — required (465 -> implicit TLS; 587/others -> STARTTLS)
* ``SMTP_USER``   — required (login + default From)
* ``SMTP_PASS``   — required
* ``REPORT_TO``   — optional, default ``himmat.aljasem@swift-assets.de``
* ``REPORT_FROM`` — optional, default ``SMTP_USER``
* ``SUMMARY_JSON_PATH`` — optional, default ``/tmp/enrich_summary.json``

No secrets are ever placed in the email body. Exit codes: 0 sent · 2 config error.
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
from email.message import EmailMessage
from typing import Any, Optional

DEFAULT_SUMMARY_PATH = "/tmp/enrich_summary.json"
DEFAULT_RECIPIENT = "himmat.aljasem@swift-assets.de"


def load_summary(path: str) -> Optional[dict[str, Any]]:
    """Read the summary JSON; None when absent/unreadable (worker likely failed)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _fmt_reasons(rejected_by_reason: Any) -> str:
    """Render the top rejected-by-reason list as indented bullet lines."""
    lines = []
    for item in (rejected_by_reason or [])[:6]:
        try:
            reason, count = item[0], item[1]
        except (TypeError, IndexError, KeyError):
            continue
        lines.append(f"   • {count}× {reason}")
    return "\n".join(lines) or "   • —"


def build_report_message(summary: Optional[dict[str, Any]], *,
                         to_addr: str, from_addr: str,
                         date_fallback: str = "") -> EmailMessage:
    """Build the EmailMessage from the summary dict (pure — unit-testable).

    A missing summary or ``ok=False`` produces a clearly-flagged failure report so
    the daily email always conveys real state.
    """
    msg = EmailMessage()
    msg["To"] = to_addr
    msg["From"] = from_addr

    if not summary:
        date = date_fallback or "غير معروف"
        msg["Subject"] = f"Swift Assets — Daily Enrichment Health Check {date} — فشل"
        msg.set_content(
            "تقرير الفحص اليومي لإثراء بيانات الشركات.\n\n"
            "⚠️ فشل: لم يُنتج عامل الإثراء أي ملخص (summary JSON) لهذا اليوم.\n"
            "راجع سجلّ تشغيل GitHub Actions لمعرفة السبب.\n")
        return msg

    date = summary.get("date") or date_fallback or "غير معروف"
    ok = summary.get("ok", True)
    status = "نجاح" if ok else "فشل"
    msg["Subject"] = (
        f"Swift Assets — Daily Enrichment Health Check {date} — {status}")

    providers = ", ".join(summary.get("providers_used") or []) or "—"
    body_lines = [
        f"تقرير الفحص اليومي لإثراء بيانات الشركات — {date}",
        "=" * 48,
        f"الحالة: {status}" + ("" if ok else f"  ({summary.get('error', '')})"),
        f"المزوّدون المستخدمون: {providers}",
        f"وضع التجربة (dry_run): {summary.get('dry_run')}",
        "",
        f"الشركات المعالجة: {summary.get('companies_processed', 0)}",
        f"عمليات بحث Serper المستخدمة: {summary.get('serper_searches_used', 0)}",
        f"مقبولة: {summary.get('accepted', 0)}  "
        f"(عالية الثقة: {summary.get('high', 0)}، متوسطة: {summary.get('medium', 0)})",
        f"مكتوبة في قاعدة البيانات: {summary.get('written', 0)}",
        f"مرفوضة: {summary.get('rejected', 0)}",
        "",
        "أهم أسباب الرفض:",
        _fmt_reasons(summary.get("rejected_by_reason")),
        "",
        "التغطية الإجمالية:",
        f"   مُثراة حتى الآن: {summary.get('total_enriched_now', 0)}",
        f"   إجمالي المؤهّلة: {summary.get('eligible_total', 0)}",
        f"   نسبة التغطية: {summary.get('coverage_percent', 0)}%",
        "",
        "(تقرير آلي — لا يحتوي على أي بيانات حسّاسة.)",
    ]
    msg.set_content("\n".join(body_lines))
    return msg


def send_message(msg: EmailMessage, *, host: str, port: int,
                 user: str, password: str) -> None:
    """Send via SMTP — implicit TLS on 465, otherwise STARTTLS."""
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=60) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.ehlo()
            s.starttls()
            s.login(user, password)
            s.send_message(msg)


def main() -> int:
    host = os.environ.get("SMTP_HOST", "").strip()
    port_raw = os.environ.get("SMTP_PORT", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "")
    if not (host and port_raw and user and password):
        print("[config] missing SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS",
              file=sys.stderr)
        return 2
    try:
        port = int(port_raw)
    except ValueError:
        print(f"[config] invalid SMTP_PORT: {port_raw!r}", file=sys.stderr)
        return 2

    to_addr = os.environ.get("REPORT_TO", "").strip() or DEFAULT_RECIPIENT
    from_addr = os.environ.get("REPORT_FROM", "").strip() or user
    summary_path = os.environ.get("SUMMARY_JSON_PATH", DEFAULT_SUMMARY_PATH)

    summary = load_summary(summary_path)
    if summary is None:
        print(f"[warn] no summary JSON at {summary_path}; sending failure report",
              file=sys.stderr)
    msg = build_report_message(summary, to_addr=to_addr, from_addr=from_addr)
    send_message(msg, host=host, port=port, user=user, password=password)
    print(f"[ok] health report sent to {to_addr} (subject: {msg['Subject']!r})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
