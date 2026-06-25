# PHASE 0037G — Live Ingestion & Sync Quality Audit (after 0037F-A)

READ-ONLY audit. No production writes, no scraper run, no portal fetch, no
migration, no backfill triggered. Snapshot: 2026-06-25, project
`hqyktreytsjeirlpnnyr`, schema `swift_v2`. HEAD `766d228`.

## Decision

**READY for Phase 0037F-B — conditioned on first closing one structural sync
gap (a 1-line workflow fix).** Data quality is good and there are no
data-integrity blockers. Rollback of 0037F-A is **NOT** needed.

## 1. Baseline after 0037F-A (counts only)

| metric | value |
|---|---:|
| apify_cases total (neu_insolvenz_direct) | 24,259 |
| swift source rows total | 22,966 |
| apify NULL type | 8,971 |
| swift NULL type | 8,922 |
| apify NULL + has text | 639 |
| swift NULL + has text | 590 |
| apify NULL + text-empty | 8,332 |
| swift NULL + text-empty | 8,332 |
| **apify typed / swift NULL (mirrored)** | **0** ✓ |
| **apify↔swift type mismatch** | **0** ✓ |
| **cockpit `unknown` from non-null hint** | **0** ✓ |
| cockpit `unknown` from NULL hint | 8,922 |
| **apify rows NOT in swift at all** | **1,293** ⚠ |

Classifier quality (global): of 15,927 text-bearing apify rows, only **639
(4.0%)** still have NULL type — the rest are genuinely unclassifiable generic
notices (`Beschluss`/`Termin`), correctly left NULL. The 0037D classifier fix
is working.

## 2. Latest-run quality (per source_run_id)

| source_run_id | rows | NULL type | NULL% | has text | missing text | gap: text&no-type | not_in_swift | workflow |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| gha-20260625T115950Z | 1293 | 49 | 3.8% | 1293 | 0 | 49 | **1293** | text-backfill (sched) |
| gha-20260625T095923Z | 2012 | 499 | 24.8% | 1534 | 478 | 21 | 0 | daily-scrape |
| gha-20260624T211400Z | 418 | 0 | 0.0% | 418 | 0 | 0 | 0 | daily-scrape |
| gha-20260624T202255Z | 485 | 76 | 15.7% | 425 | 60 | 16 | 0 | text-backfill |
| gha-20260624T120527Z | 407 | 60 | 14.7% | 362 | 45 | 15 | 0 | text-backfill |
| gha-20260624T102825Z | 2073 | 334 | 16.1% | 1816 | 257 | 77 | 0 | daily-scrape |
| gha-20260623T210621Z | 513 | 26 | 5.1% | 512 | 1 | 25 | 0 | daily-scrape |
| gha-20260623T122501Z | 511 | 22 | 4.3% | 511 | 0 | 22 | 0 | text-backfill |

Reading: the **classifier gap (text exists but type NULL)** is now ~1.4–4% per
run. Higher overall NULL% (e.g. 24.8%) is driven by **missing_text**
(detail-fetch coverage), NOT classifier failure. `detail_fetch_attempts` is 0
for all rows in every recent run → missing-text rows were budget-skipped, not
exhausted (so they remain re-fetchable for 0037F-B).

## 3. Sync quality — the structural finding

The apify→swift sync (`swift_v2.backfill_neu_insolvenz_direct_entities`) is
invoked by **`scripts/run_swift_v2_backfill.py`**, but only from
**`daily-scrape.yml`** (step "Run Swift V2 entity backfill", `if: success()`,
2×/day). **`text-backfill.yml` runs `scraper.py` 4×/day (02/08/14/20 UTC +
dispatch) and has NO sync step at all.**

Consequence — eventual consistency with up to ~12h lag:
- **1,293 apify rows not in swift** = the latest text-backfill run (run 13,
  11:59Z), all text-bearing, 1,244 already typed — never synced because
  text-backfill doesn't call the sync. They will land on the next daily-scrape.
- **1,534 mirrored swift rows are text-stale** (apify has text, swift still
  empty) — text added by text-backfill runs, swift text-update pending the next
  daily backfill (the sync's re-sync predicate covers them once it runs).
- Among rows the sync HAS processed: type backlog = 0, mismatch = 0 — i.e. the
  sync logic is correct; the gap is purely *when* it is triggered.

A manual mitigation exists: **`backfill-only.yml`** runs the sync on demand.

## 4. 0037F-B readiness

**Data quality:** READY. Classifier gap 4%, type sync integrity perfect,
cockpit `unknown` = exactly the NULL-type set, text-empty backlog bounded
(8,332 rows / 8 distinct days, mostly attempts=0 → reachable).

**Blocker (operational, not integrity):** `text-backfill.yml` re-fetches
text into `apify_cases` but does not sync to `swift_v2`. 0037F-B's entire point
is to get text+types for the 8,332 empty rows *into the cockpit* — without the
sync step, the re-fetched data would sit in apify and lag. Note: 0037F-B's core
work (detail re-fetch of the empty-text backlog) is **already automated** by
text-backfill.yml; the missing piece is the sync trigger.

**Race risk:** 6 scraper runs/day already hit the portal. A manual 0037F-B
scraper invocation outside the `text-backfill` concurrency group could race /
double-fetch. Prefer driving 0037F-B through the existing text-backfill
workflow (which has `concurrency: group: text-backfill`) rather than an ad-hoc run.

## 5. Recommendation

1. **First (highest leverage, low risk):** add the existing "Run Swift V2 entity
   backfill" step (`python scripts/run_swift_v2_backfill.py`, `if: success()`)
   to **`text-backfill.yml`**, mirroring `daily-scrape.yml`. This closes the
   structural lag so every text-backfill run delivers to the cockpit. As a
   one-time catch-up, run `backfill-only.yml` to drain the current 1,293 + 1,534.
2. **Then 0037F-B** is effectively continuous via text-backfill; just confirm
   the empty-text backlog (8,332) drains over subsequent runs and watch the
   classifier gap stays low. No separate risky manual re-fetch is required.

Rollback of 0037F-A: not needed (type integrity verified: mismatch 0, cockpit
non-null→unknown 0, no text-empty rows were touched).
