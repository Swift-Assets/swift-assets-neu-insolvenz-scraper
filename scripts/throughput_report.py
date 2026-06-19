#!/usr/bin/env python3
"""
throughput_report.py
====================

Daily performance / capacity tracker for the Swift Assets insolvency pipeline.

What it does
------------
1. Pulls, per day for the last 30 days (Europe/Berlin calendar days):
     * successful insolvency scrape volume  (rows ingested into
       swift_v2.source_neu_insolvenz_announcements, by created_at)
     * Handelsregister enrichment completions (rows written into
       swift_v2.source_handelsregister_records, by fetched_at/created_at)
2. Plots the daily volume trend (last 30 days) -> PNG.
3. Drafts a one-page Markdown report comparing ACTUAL throughput against the
   THEORETICAL capacity of the current processing window, and flags whether the
   window is becoming a constraint as volume grows.

Theoretical capacity (current production config)
-------------------------------------------------
* Insolvency runs/day .............. 2   (18:00 + 00:05 Europe/Berlin)
* Insolvency detail-fetch cap/run .. 1500  (MAX_DETAIL_FETCHES)
    => theoretical insolvency detail capacity/day = 2 * 1500 = 3000
* Handelsregister enrichment/run ... 180   (legal-safe batch)
    => theoretical HR enrichment capacity/day = 2 * 180 = 360
* HR legal rate cap ................ 60 requests/hour per IP (handelsregister.de)
* HR effective pace ................ ~46 req/h (75s base + 5s jitter)

These ceilings can be overridden via env vars (see CONFIG below) so the report
stays correct if you later change the schedule or per-run limits.

Environment
-----------
- SUPABASE_URL                  (required)
- SUPABASE_SERVICE_ROLE_KEY     (required)  -- read-only use; never logged/written
Optional capacity overrides:
- INS_RUNS_PER_DAY      (default 2)
- INS_DETAIL_CAP_PER_RUN(default 1500)
- HR_BATCH_PER_RUN      (default 180)
- HR_RUNS_PER_DAY       (default 2)
- HR_LEGAL_RATE_PER_HOUR(default 60)
- REPORT_DAYS           (default 30)
- OUTPUT_DIR            (default ./reports)

Outputs (in OUTPUT_DIR)
-----------------------
- throughput_trend.png
- throughput_report.md
- throughput_daily.csv

Exit codes: 0 ok, 2 config error, 3 transport error, 4 bad HTTP from PostgREST.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from urllib import error, request

SCHEMA = "swift_v2"


def _fail(code: int, msg: str) -> None:
    print(f"[throughput_report] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _fail(2, f"{name} must be an integer, got {raw!r}")


def _redact(s: str) -> str:
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    return s.replace(key, "***REDACTED***") if key and key in s else s


def run_rpc_sql(supabase_url: str, service_key: str, sql: str) -> list[dict]:
    """Execute a read-only SQL statement via a generic exec RPC if available,
    otherwise fall back to PostgREST table reads. We avoid assuming an exec RPC
    exists; instead we use dedicated REST aggregation through a SQL view created
    at runtime is NOT possible from here, so we use PostgREST's built-in
    aggregation via the REST API per-table. See query_daily() below."""
    raise NotImplementedError


def _rest_get(supabase_url: str, service_key: str, path: str) -> list[dict]:
    url = f"{supabase_url}/rest/v1/{path}"
    req = request.Request(
        url,
        method="GET",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Accept": "application/json",
            "Accept-Profile": SCHEMA,
        },
    )
    try:
        with request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        _fail(4, _redact(f"HTTP {e.code} on {path}: {body}"))
    except Exception as e:  # noqa: BLE001
        _fail(3, _redact(f"transport error on {path}: {e}"))
    return []


def daily_counts(rows: list[dict], ts_field: str, tz_offset_hours: int, days: int) -> dict[date, int]:
    """Bucket ISO-timestamp rows into Berlin calendar days."""
    counts: dict[date, int] = {}
    for r in rows:
        raw = r.get(ts_field)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(timezone(timedelta(hours=tz_offset_hours)))
        d = local.date()
        counts[d] = counts.get(d, 0) + 1
    return counts


def main() -> None:
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not supabase_url.startswith("https://"):
        _fail(2, "SUPABASE_URL missing or not https://")
    if not service_key:
        _fail(2, "SUPABASE_SERVICE_ROLE_KEY missing")

    days = _env_int("REPORT_DAYS", 30)
    ins_runs = _env_int("INS_RUNS_PER_DAY", 2)
    ins_cap = _env_int("INS_DETAIL_CAP_PER_RUN", 1500)
    hr_batch = _env_int("HR_BATCH_PER_RUN", 180)
    hr_runs = _env_int("HR_RUNS_PER_DAY", 2)
    hr_rate = _env_int("HR_LEGAL_RATE_PER_HOUR", 60)
    out_dir = os.environ.get("OUTPUT_DIR", "reports").strip() or "reports"
    os.makedirs(out_dir, exist_ok=True)

    # Berlin is UTC+2 in summer (CEST). We bucket by a fixed +2 offset; this is
    # a reporting approximation and is documented in the report footnotes.
    tz_offset = 2

    since = (datetime.now(timezone.utc) - timedelta(days=days + 1)).date().isoformat()

    # Pull only timestamps we need, filtered server-side by date, with a high cap.
    ins_rows = _rest_get(
        supabase_url, service_key,
        f"source_neu_insolvenz_announcements?select=created_at&created_at=gte.{since}&limit=200000",
    )
    hr_rows = _rest_get(
        supabase_url, service_key,
        f"source_handelsregister_records?select=fetched_at,created_at,status&limit=200000",
    )

    ins_daily = daily_counts(ins_rows, "created_at", tz_offset, days)
    # HR completion = a record was written. Prefer fetched_at, fall back to created_at.
    for r in hr_rows:
        if not r.get("fetched_at"):
            r["fetched_at"] = r.get("created_at")
    hr_daily = daily_counts(hr_rows, "fetched_at", tz_offset, days)

    today_local = datetime.now(timezone(timedelta(hours=tz_offset))).date()
    window = [today_local - timedelta(days=i) for i in range(days - 1, -1, -1)]

    ins_series = [ins_daily.get(d, 0) for d in window]
    hr_series = [hr_daily.get(d, 0) for d in window]

    # ---- capacity math ----
    ins_capacity = ins_runs * ins_cap          # detail fetches/day
    hr_capacity = hr_runs * hr_batch            # enrichments/day

    active_ins = [v for v in ins_series if v > 0]
    active_hr = [v for v in hr_series if v > 0]

    def _avg(xs): return round(sum(xs) / len(xs), 1) if xs else 0.0
    def _peak(xs): return max(xs) if xs else 0

    ins_avg, ins_peak = _avg(active_ins), _peak(ins_series)
    hr_avg, hr_peak = _avg(active_hr), _peak(hr_series)

    ins_peak_util = round(100 * ins_peak / ins_capacity, 1) if ins_capacity else 0
    hr_peak_util = round(100 * hr_peak / hr_capacity, 1) if hr_capacity else 0
    ins_avg_util = round(100 * ins_avg / ins_capacity, 1) if ins_capacity else 0
    hr_avg_util = round(100 * hr_avg / hr_capacity, 1) if hr_capacity else 0

    # ---- CSV ----
    csv_path = os.path.join(out_dir, "throughput_daily.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date_berlin", "insolvency_ingested", "handelsregister_enriched"])
        for d, a, b in zip(window, ins_series, hr_series):
            w.writerow([d.isoformat(), a, b])

    # ---- chart ----
    png_path = os.path.join(out_dir, "throughput_trend.png")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.ticker import MaxNLocator

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7.2), sharex=True)
        xs = list(range(len(window)))
        labels = [d.strftime("%m-%d") for d in window]

        ax1.bar(xs, ins_series, color="#2563eb", label="Insolvency ingested/day")
        ax1.axhline(ins_capacity, color="#dc2626", ls="--", lw=1.4,
                    label=f"Theoretical cap {ins_capacity}/day ({ins_runs}×{ins_cap})")
        ax1.set_ylabel("Insolvency rows")
        ax1.set_title("Daily Insolvency Ingestion vs Theoretical Detail-Fetch Capacity")
        ax1.legend(fontsize=8, loc="upper left")
        ax1.grid(axis="y", alpha=0.3)
        ax1.yaxis.set_major_locator(MaxNLocator(integer=True))

        ax2.bar(xs, hr_series, color="#059669", label="HR enrichments/day")
        ax2.axhline(hr_capacity, color="#dc2626", ls="--", lw=1.4,
                    label=f"Theoretical cap {hr_capacity}/day ({hr_runs}×{hr_batch})")
        ax2.set_ylabel("HR records")
        ax2.set_title("Daily Handelsregister Enrichment vs Theoretical Capacity")
        ax2.legend(fontsize=8, loc="upper left")
        ax2.grid(axis="y", alpha=0.3)
        ax2.yaxis.set_major_locator(MaxNLocator(integer=True))

        step = max(1, len(window) // 15)
        ax2.set_xticks(xs[::step])
        ax2.set_xticklabels(labels[::step], rotation=45, ha="right", fontsize=8)
        fig.suptitle(f"Swift Assets Pipeline Throughput — last {days} days "
                     f"(generated {today_local.isoformat()})", fontsize=12, y=0.99)
        fig.tight_layout(rect=[0, 0, 1, 0.97])
        fig.savefig(png_path, dpi=130)
        plt.close(fig)
        chart_ok = True
    except Exception as e:  # noqa: BLE001
        print(f"[throughput_report] WARN: chart skipped: {e}", file=sys.stderr)
        chart_ok = False

    # ---- constraint verdict ----
    def verdict(peak_util: float) -> str:
        if peak_util >= 90:
            return "🔴 CONSTRAINT — peak demand is at/above 90% of capacity. Add a run or raise the per-run cap."
        if peak_util >= 70:
            return "🟠 WATCH — peak demand exceeds 70% of capacity. Plan to add headroom soon."
        if peak_util >= 40:
            return "🟡 HEALTHY — comfortable headroom, monitor the trend."
        return "🟢 AMPLE — demand is well below capacity."

    days_with_data = sum(1 for v in ins_series if v > 0)

    md = []
    md.append("# Swift Assets — Pipeline Throughput & Capacity Report")
    md.append("")
    md.append(f"**Generated:** {datetime.now(timezone(timedelta(hours=tz_offset))).strftime('%Y-%m-%d %H:%M')} Europe/Berlin  ")
    md.append(f"**Window:** last {days} days · **Days with data:** {days_with_data}")
    md.append("")
    md.append("## 1. Headline")
    md.append("")
    md.append("| Stream | Avg/active day | Peak/day | Theoretical cap/day | Peak utilization | Status |")
    md.append("|---|--:|--:|--:|--:|---|")
    md.append(f"| Insolvency ingestion | {ins_avg} | {ins_peak} | {ins_capacity} | {ins_peak_util}% | {verdict(ins_peak_util).split(' — ')[0]} |")
    md.append(f"| Handelsregister enrichment | {hr_avg} | {hr_peak} | {hr_capacity} | {hr_peak_util}% | {verdict(hr_peak_util).split(' — ')[0]} |")
    md.append("")
    md.append("## 2. Actual vs Theoretical Capacity")
    md.append("")
    md.append("**Insolvency stream**")
    md.append("")
    md.append(f"- Theoretical detail-fetch capacity: **{ins_runs} runs/day × {ins_cap} fetches = {ins_capacity}/day**")
    md.append(f"- Actual peak ingested in a day: **{ins_peak}** ({ins_peak_util}% of cap); avg on active days: **{ins_avg}** ({ins_avg_util}% of cap)")
    md.append(f"- {verdict(ins_peak_util)}")
    md.append("")
    md.append("> Note: the scraper saves *core records* for every announcement regardless of the detail-fetch cap. "
              "The cap only limits the deeper per-record detail enrichment, so ingestion above the cap is possible "
              "while still leaving some records without full detail.")
    md.append("")
    md.append("**Handelsregister stream**")
    md.append("")
    md.append(f"- Theoretical enrichment capacity: **{hr_runs} runs/day × {hr_batch} companies = {hr_capacity}/day**")
    md.append(f"- Actual peak enriched in a day: **{hr_peak}** ({hr_peak_util}% of cap); avg on active days: **{hr_avg}** ({hr_avg_util}% of cap)")
    md.append(f"- Legal rate ceiling: **{hr_rate} requests/hour per IP** (handelsregister.de Nutzungsordnung, §9 HGB). "
              f"Per-run batch of {hr_batch} at ~46 req/h ≈ {round(hr_batch/46,1)} h, within the 6 h GitHub-runner cap.")
    md.append(f"- {verdict(hr_peak_util)}")
    md.append("")
    md.append("## 3. Is the processing window becoming a constraint?")
    md.append("")
    if hr_peak_util >= 90 or ins_peak_util >= 90:
        md.append("**Yes — at least one stream is hitting its ceiling.** Recommended next steps, in order of preference:")
        md.append("")
        md.append(f"1. **Handelsregister:** the {hr_capacity}/day ceiling is fixed by the 60 req/h legal cap, *not* by GitHub. "
                  "Adding insolvency runs will **not** raise HR throughput (HR is rate-limited and serialized). "
                  "To drain a growing HR backlog, add a *dedicated, separately-spaced* HR-only scheduled run that still respects 60/h.")
        md.append("2. **Insolvency:** if ingestion peaks exceed the detail-fetch cap, raise `MAX_DETAIL_FETCHES` rather than adding runs.")
    else:
        md.append("**Not yet.** Both streams are operating below their capacity ceilings. The current "
                  f"window ({ins_runs}× insolvency + {hr_runs}× HR daily) has headroom. Keep monitoring this report; "
                  "act when peak utilization crosses 70% (WATCH) and certainly before 90% (CONSTRAINT).")
    md.append("")
    md.append("**Key structural fact:** Handelsregister capacity is bounded by law (60 req/h), so it is the *binding* "
              "constraint as volume grows — not the insolvency scraper and not the GitHub runner. When the daily insolvency "
              "company volume that needs HR verification consistently exceeds "
              f"**{hr_capacity}/day**, the HR queue will grow without bound under the current schedule.")
    md.append("")
    md.append("## 4. Trend")
    md.append("")
    if chart_ok:
        md.append("![Throughput trend](throughput_trend.png)")
    else:
        md.append("_(chart unavailable in this run — see throughput_daily.csv)_")
    md.append("")
    md.append("## 5. Method & caveats")
    md.append("")
    md.append("- Insolvency volume = rows in `swift_v2.source_neu_insolvenz_announcements` bucketed by `created_at`.")
    md.append("- HR completions = rows in `swift_v2.source_handelsregister_records` bucketed by `fetched_at` (fallback `created_at`).")
    md.append("- Days bucketed to Europe/Berlin using a fixed UTC+2 (CEST) offset; off by up to 1 h around midnight in winter.")
    md.append("- Capacity ceilings are config-driven (env overrides) so the report tracks any future schedule/limit change.")
    md.append("")
    md.append("_Raw daily numbers: `throughput_daily.csv`._")

    md_path = os.path.join(out_dir, "throughput_report.md")
    with open(md_path, "w") as f:
        f.write("\n".join(md) + "\n")

    print(f"[throughput_report] Wrote: {md_path}")
    print(f"[throughput_report] Wrote: {csv_path}")
    if chart_ok:
        print(f"[throughput_report] Wrote: {png_path}")
    print(f"[throughput_report] Insolvency peak {ins_peak}/{ins_capacity} ({ins_peak_util}%), "
          f"HR peak {hr_peak}/{hr_capacity} ({hr_peak_util}%).")


if __name__ == "__main__":
    main()
