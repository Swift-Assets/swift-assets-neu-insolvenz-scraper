# Enrichment Pipeline Fix Report

**Date:** 2026-06-21 · **Author:** automated investigation (Claude Code) · **DB:** Supabase `hqyktreytsjeirlpnnyr`, schema `swift_v2`

**Pull requests**
- Scraper (backlog pagination): https://github.com/Swift-Assets/swift-assets-neu-insolvenz-scraper/pull/15
- Web portal (mirror propagation + daily run log): https://github.com/Swift-Assets/swift-assets-04-web-portal/pull/6
- Handelsregister (review queue + backoff): https://github.com/Swift-Assets/swift-assets-handelsregister-scraper/pull/7

---

## 1. ملخّص تنفيذي

كان "إثراء التفاصيل" يعمل فعلاً ويكتب نصّ الإعلان ونوعه وبيانات وكيل الإعسار في جدول `public.apify_cases`، لكن دالّة المزامنة `swift_v2.backfill_neu_insolvenz_direct_entities` كانت تنسخ الصف مرّة واحدة فقط عند أول إدخال ثم لا تعيد اختياره أبداً، فلم تصل بيانات الإثراء المتأخّرة إلى الجدول الذي تقرأه البوّابة (`source_neu_insolvenz_announcements`) — لذلك بقيت ~98–99% من الصفوف فارغة و`enrichment_status='pending'`. أصلحنا الدالّة لتعيد مزامنة الصفوف المُثراة، وشغّلنا تعبئة رجعية (idempotent) عالجت كل الـ11,085 صفّاً، فارتفع `enrichment_status=done` من 0 إلى 1,794 وتحسّن استنتاج المرحلة وتغطية اسم الوكيل. كذلك أصلحنا حارس الساعة في تقرير `daily_run_log` (لم يكن يُسجَّل منذ 2026-06-16)، وأضفنا طابوراً للمراجعة مع backoff في كاشطة Handelsregister. كما عالجنا سبباً ثانوياً في الكاشطة الرئيسية يحدّ من سحب المتراكم عند 1000 صفّ بسبب سقف PostgREST.

---

## 2. Root cause

### 2.1 Primary — enrichment never propagated to the portal mirror
The daily scrape and the every-6h `Historical Text Backfill` workflow both run and succeed, and they **do** enrich announcements — but they write the enriched columns to `public.apify_cases`. The portal reads `swift_v2.source_neu_insolvenz_announcements`, which is populated from apify_cases by the RPC `swift_v2.backfill_neu_insolvenz_direct_entities(p_limit)` (called by [`scripts/run_swift_v2_backfill.py`](scripts/run_swift_v2_backfill.py)).

That RPC's `src` selector treated a row as "needs work" only while it was **(a)** not yet ingested, **(b)** un-linked (`entity_id IS NULL`), or **(c)** missing an `entity_source_links` row. Once a row was ingested **and** linked it was **never re-selected** — confirmed by the script's own docstring ("it only selects apify_cases rows that still need work: not yet ingested, OR entity_id is null, OR missing their entity_source_links row"). So enrichment that landed in apify_cases *after* the first sync never reached the mirror.

Evidence at time of diagnosis (live DB):
- `apify_cases` (neu_insolvenz_direct): 1,794 rows with `announcement_text`; mirror had only **1,206** — **588 enriched rows stuck**, plus 537 type / 92 admin stuck.
- `source_neu_insolvenz_announcements`: **11,079 / 11,079** rows had `enrichment_status='pending'` — nothing ever transitions it (the scrapers never write that field; it is frozen at insert from `apify_cases.enrichment_status`, which is itself always `'pending'`).

### 2.2 Secondary — apify-side backlog drainer capped at 1000 rows
`SupabaseClient.fetch_empty_text_backlog` ([`scraper.py`](scraper.py)) issued a **single** `Range` request expecting up to `server_limit` (≤8000) rows. PostgREST enforces a server-side `max-rows` ceiling (1000) that `Range` does **not** override. Production log: `fetch_empty_text_backlog: server returned 1000 candidate rows (server_limit=8000)`. So the drainer only ever saw the oldest 1000 empty-text rows; with ~9k empty, **8,840 rows had `detail_fetch_attempts=0` (never attempted)**. (Source site also returns frequent `500`s on re-search submits — environmental, already retried 3× with backoff.)

### 2.3 Secondary — daily_run_log not written since 2026-06-16
[`scripts/automation/daily-v2-report.mjs`](https://github.com/Swift-Assets/swift-assets-04-web-portal) line ~198 guarded with `if (hour !== '18') process.exit(0)`. GitHub delivers scheduled events hours late (observed runs at ~21:00–22:00 UTC), so the Berlin hour is essentially never exactly `18` → every scheduled run skipped. The only `daily_run_log` row (2026-06-16) came from a manual `FORCE_RUN` self-test.

### 2.4 Secondary — Handelsregister permanent failures retried forever
In `handelsregister_scraper.py` the per-entity loop swallowed `TransientNetworkError` and non-structural `RuntimeError` with a bare `continue` and **no DB write**, so a failing entity stayed in `v_handelsregister_pending_queue` and consumed a rate-limited request every run indefinitely (the recurring `TimeoutError` churn).

---

## 3. Changes made

### `swift-assets-04-web-portal` — PR #6
| File | Change |
|---|---|
| `supabase/migrations/swift_v2_0015_resync_enrichment_into_mirror.sql` | `CREATE OR REPLACE` the RPC: add a 4th `src` branch that re-selects already-linked rows whose apify_cases enrichment is newer than the mirror (or whose terminal status should change), and stamp `enrichment_status` = `done`/`failed`/`pending`. Idempotent + resumable. |
| `scripts/automation/daily-v2-report.mjs` | Loosen the guard from `hour !== '18'` to `hour < '18'` (skip only *before* 18:00 Berlin); DB advisory lock + one-success-per-day unique index already prevent duplicates. |

### `swift-assets-neu-insolvenz-scraper` — PR #15
| File | Change |
|---|---|
| `scraper.py` (`fetch_empty_text_backlog`) | Paginate with successive `Range` requests past the PostgREST 1000-row `max-rows` cap; deterministic order `announcement_date.asc,id.asc`; 50-page safety bound. Drains up to the full reserved budget per run instead of 1000. |
| `ENRICHMENT_FIX_REPORT.md` | This report. |

### `swift-assets-handelsregister-scraper` — PR #7
| File | Change |
|---|---|
| `handelsregister_scraper.py` | On exhausted retries, record a `search_error` event (dedicated `::error` external id, best-effort, never aborts the run). Structural form errors excluded (global signal, handled elsewhere). |
| `sql/2026_06_21_patch_v5_search_error_review_queue.sql` | `search_error` backoff in `v_handelsregister_pending_queue` (24h fresh / 7d once persistent ≥3 days) + new `v_handelsregister_error_review` view. 60 req/h legal cap unchanged. |

---

## 4. Backfill

**What was processed:** the propagation backlog in `swift_v2.source_neu_insolvenz_announcements`.

**How:** the fixed RPC was applied to prod (`apply_migration swift_v2_0015`) and drained in bounded batches via `SELECT * FROM swift_v2.backfill_neu_insolvenz_direct_entities(3000)` — the same path `scripts/run_swift_v2_backfill.py` uses in CI.

- Batch 1: `source_rows_changed = 1800` (re-synced 1,794 enriched rows to `done` + ingested 6 previously-missing rows).
- Batch 2: `source_rows_changed = 0` → **converged**, confirming idempotency.

**Duration:** seconds (single DB-side function; ~1,800 rows). The ongoing apify→mirror sync rides the existing daily/text-backfill workflows.

**Resume if interrupted:** re-run `backfill_neu_insolvenz_direct_entities(<batch>)` (or `run_swift_v2_backfill.py`) — it is forward-progressing and returns `source_rows_changed = 0` when nothing is stale. Safe to run any number of times.

> Note: the deeper **detail-fetch** backlog (8,840 apify rows never attempted) is drained by the scrapers over time; PR #15 unblocks it by removing the 1000-row cap. It is not a one-shot DB operation.

---

## 5. Before / after metrics

```sql
-- (1) enrichment_status distribution
SELECT raw_json->>'enrichment_status' AS status, count(*)
FROM swift_v2.source_neu_insolvenz_announcements GROUP BY 1;

-- (2) phase distribution
SELECT phase_inferred, count(*) FROM swift_v2.v_public_company_search GROUP BY 1 ORDER BY 2 DESC;

-- (3) administrator coverage
SELECT count(*) FILTER (WHERE insolvency_administrator IS NOT NULL) AS with_admin, count(*) AS total
FROM swift_v2.v_public_company_detail_v2;
```

| Metric | Before | After |
|---|---|---|
| `enrichment_status = done` | 0 | **1,794** |
| `enrichment_status = pending` | 11,079 | 9,291 |
| `phase_inferred` non-`unknown` (search view) | ~35 of 946 | **58** of 934 (opening 27, preliminary_administration 24, administrator_appointed 7) |
| Administrator coverage (`v_public_company_detail_v2`) | ~15 of 981 | **20** of 934 |
| Mirror rows with `announcement_text` | 1,206 | **1,794** |
| Mirror rows with `announcement_type_hint` | 1,098 | **1,635** |
| Mirror rows with `insolvency_administrator` | 220 | **312** |
| Enriched text "stuck" in apify (not propagated) | 588 | **0** |

The portal-facing numbers (phase, admin coverage) improve modestly because they are bounded by how much apify-side enrichment exists today (~1,794 of 11,085 announcements have text). They will keep rising automatically as the scrapers enrich more — the propagation layer is now correct and self-healing.

---

## 6. Remaining risks / follow-ups

1. **apify-side detail-fetch throughput (main follow-up).** 8,840 rows still have no text. PR #15 removes the 1000-row cap, but the source site `500`s frequently on re-search submits, capping real throughput. *Recommend:* monitor `Historical Text Backfill` run logs after PR #15 merges; consider lowering worker concurrency / adding inter-request jitter if 500s persist.
2. **Admin sub-fields parser.** `insolvency_admin_email/phone/address` exist in apify_cases and now ride along in the mirror's `raw_json`, but there is no structured parser populating dedicated columns. *Recommend:* add a parser if the portal needs them as first-class fields.
3. **Handelsregister change needs a live validation run.** The review-queue routing could not be unit-tested here (no Python in the authoring env). *Recommend:* dispatch the workflow once with `dry_run=true`, then a small `limit`, and confirm `v_handelsregister_error_review` populates on a forced failure.
4. **daily_run_log on extreme delay.** If GitHub delays a scheduled run past midnight Berlin, the loosened guard still skips that day. Rare; the DB idempotency prevents any harm. *Optional:* switch to a date-based claim independent of wall-clock hour.
5. **`enrichment_status` semantics.** It is now derived (text present → done; attempts ≥5 → failed). If a separate AI-enrichment stage later needs its own status, use a distinct field rather than overloading this one.

---

## 7. Rollback

- **Portal RPC (swift_v2_0015):** re-apply the pre-0015 function definition (drop the 4th `src` branch; revert `raw_json` to plain `to_jsonb(src)`). Rows already re-synced stay corrected (desired); no data is lost either way.
- **Portal daily-report guard:** revert the one-line change in `daily-v2-report.mjs` (or just revert PR #6's commit).
- **Scraper pagination (PR #15):** revert the `fetch_empty_text_backlog` change; behaviour returns to the single-request (1000-cap) fetch.
- **Handelsregister (PR #7):** re-apply `sql/2026_06_18_patch_v4_...` to restore the v4 pending-queue view and `DROP VIEW IF EXISTS swift_v2.v_handelsregister_error_review;`. Revert the `handelsregister_scraper.py` commit to stop writing `search_error` events (existing rows are harmless).

All four changes are independently revertible; none deletes data, disables RLS, or exposes `service_role`.
