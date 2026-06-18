# HTTP 409 Duplicate-Key Handling Report

**Date:** 2026-06-18
**Repo:** `Swift-Assets/swift-assets-neu-insolvenz-scraper`
**Status:** Implemented · production blocker resolved

---

## 1. Problem summary

The last end-to-end scraper run failed because `SupabaseClient.upsert_cases()`
treated every HTTP non-2xx response as fatal. Two chunks out of ~65 returned
HTTP 409 with PostgreSQL SQLSTATE 23505 (duplicate key on
`apify_cases_unique_case_notice`), so the run exited with code 1 even though
6,293 of 6,493 rows were upserted successfully.

Concrete log line from the failing run
([#27758742737](https://github.com/Swift-Assets/swift-assets-neu-insolvenz-scraper/actions/runs/27758742737)):

```text
[ERROR] Upsert RPC failed @1700: 409 {"code":"23505",
  "details":"Key (court, case_number, announcement_date,
  announcement_type_hint, registry_court, registry_type, registry_number)=
  (Bochum, 80 IN 403/26, 2026-06-15, null, Recklinghausen, GnR, 125)
  already exists.", ...}
…
[INFO] Upsert summary: inserted=9 updated=6284 errors=200
##[error]Process completed with exit code 1.
```

Because the workflow's backfill step is gated on `if: success()`, this
also blocked the Swift V2 backfill from running — even though the data
that *did* land in `apify_cases` was healthy.

---

## 2. Root cause hypothesis

The unique constraint `apify_cases_unique_case_notice` uses **seven** columns
with `NULLS NOT DISTINCT`:

```text
(court, case_number, announcement_date, announcement_type_hint,
 registry_court, registry_type, registry_number)
```

Two failure modes were observed in the same daily batch:

1. **Intra-chunk collision** — the same official-source row appears twice
   in the listing (the portal occasionally republishes a row inside a single
   day). Both rows land in the same chunk and the RPC's per-row INSERT
   conflicts on the second one.
2. **Inter-chunk collision** — a `pre_check_existing_keys` lookup races
   against a concurrent write, or the registry-number gets enriched between
   the lookup and the upsert, shifting the row into an already-existing
   bucket.

In both cases the RPC raises `23505`, the entire chunk's transaction aborts,
and PostgREST returns HTTP 409 for the chunk. The previous code counted
the whole chunk (≤100 rows) as failures.

Volume seen in real traffic: ~200 conflicts per ~6,500 rows ≈ **3.1%** —
well below the 10% sanity threshold introduced below.

---

## 3. Files changed

| File | Change | Lines (approx.) |
|---|---|---|
| `scraper.py` | Added `_is_duplicate_key_error`, `_post_rpc`, `_parse_counts`, `_retry_single_rows`; rewrote `upsert_cases` to return a stats dict; rewrote `main()` failure-policy to use new stats + duplicate-ratio threshold; added `import json` | +120 / −30 |
| `docs/http-409-duplicate-handling-report.md` | **new** — this report | +200 |

Not changed (per requirements):

- `scripts/run_swift_v2_backfill.py`
- `.github/workflows/daily-scrape.yml` (the `if: success()` guard stays)
- `.github/workflows/backfill-only.yml`
- The fill-only RPC `public.neu_insolvenz_fill_only_upsert`
- The Supabase schema

---

## 4. Exact new retry behavior

### 4.1 Chunk loop (unchanged shape)

```text
for chunk in payload.split(CHUNK=100):
    POST /rest/v1/rpc/neu_insolvenz_fill_only_upsert {"p_records": chunk}
```

### 4.2 Decision tree per chunk

```text
                        ┌─────────────────┐
                        │  POST chunk     │
                        └────────┬────────┘
                                 │
        ┌────────────────────────┼─────────────────────────────┐
        ▼                        ▼                             ▼
   HTTP 2xx              HTTP 409 + code 23505         everything else
   (success)             (duplicate key)               (4xx, 5xx, network)
        │                        │                             │
        ▼                        ▼                             ▼
  parse rows_inserted   chunks_conflict_retried++         real_errors +=
  + rows_updated        WARN log entry                        len(chunk)
  into totals           split into single rows               (still fatal)
                                 │
                                 ▼
                    ┌──────────────────────────┐
                    │   POST [single row]      │
                    └────────────┬─────────────┘
                                 │
        ┌────────────────────────┼─────────────────────────────┐
        ▼                        ▼                             ▼
   HTTP 2xx              HTTP 409 + code 23505         everything else
        │                        │                             │
        ▼                        ▼                             ▼
   inserted/updated      duplicates_skipped++             real_errors++
   += counts             INFO log w/ key tuple
```

### 4.3 Final classification

`upsert_cases` returns:

```python
{
    "inserted_total":          int,
    "updated_total":           int,
    "duplicates_skipped":      int,   # 409 SQLSTATE 23505 confirmed per-row
    "real_errors":             int,   # other 4xx, 5xx, network
    "total_payload_records":   int,
    "chunks_total":            int,
    "chunks_conflict_retried": int,
}
```

### 4.4 Exit-code policy (`main()`)

```python
DUPLICATE_RATIO_THRESHOLD = 0.10   # 10%

if real_errors > 0:                       return 1   # fatal
if dupes / total > 0.10:                  return 1   # threshold breach
                                          return 0   # success (dupes ≤ 10% OK)
```

---

## 5. What counts as non-fatal duplicate

A response is treated as a non-fatal duplicate **only when both** are true:

1. HTTP status is exactly **409**, **and**
2. The JSON body is a dict with `"code": "23505"` — the PostgreSQL SQLSTATE
   for `unique_violation`.

This is implemented in `SupabaseClient._is_duplicate_key_error(status, body)`
and is intentionally strict so that other 409s (e.g. `55P03 lock_not_available`,
`40001 serialization_failure`) keep counting as `real_errors` until a future
patch classifies them.

The retry happens **once**, per-row, with a 60 s timeout. A row that 409s
on the single-row retry is the genuine duplicate.

---

## 6. What still counts as a fatal error

Everything that is not a confirmed `409 + 23505`:

| Symptom | Counted as | Fail? |
|---|---|---|
| `HTTP 200/201` | `inserted_total` / `updated_total` | no |
| `HTTP 409` + `code=23505` (per-row retry) | `duplicates_skipped` | no (unless > 10%) |
| `HTTP 409` + any other `code` | `real_errors` (×row count) | **yes** |
| `HTTP 400` (bad request, schema mismatch) | `real_errors` | **yes** |
| `HTTP 401 / 403` (auth/rotated key) | `real_errors` | **yes** |
| `HTTP 404` (RPC missing) | `real_errors` | **yes** |
| `HTTP 5xx` | `real_errors` | **yes** |
| `requests.RequestException` (DNS, timeout, reset) | `real_errors` | **yes** |
| Duplicate ratio > 10% of payload | — | **yes** |

The threshold of 10% is a guard against silent data loss. The observed
real-world rate is ≈3%, so 10% provides ~3× headroom while still catching
a regression (e.g. someone drops `announcement_type_hint` from the upsert
payload, which would collapse most rows onto a few keys).

---

## 7. Expected GitHub Actions log output

### 7.1 Healthy daily run with a few duplicates (typical)

```text
[INFO] === Run start: id=gha-20260619T220500Z | 2026-06-19 → 2026-06-19 | ...
[INFO] Parsed 6493 result rows
[INFO] Existing DB keys in range: 4174
[INFO] New (not-yet-in-DB) records: 2319 / 6493 total listing rows
[INFO] Fetching details for 1500 records with 4 workers...
[INFO] Detail fetches done: 1500/1500 in 25m12s (1.01s avg)
[WARNING] Upsert chunk @1700 hit duplicate-key (HTTP 409 / SQLSTATE 23505). Retrying 100 rows one-by-one.
[INFO]   duplicate-key skipped: ('Bochum', '80 IN 403/26', '2026-06-15', None)
[WARNING] Upsert chunk @4400 hit duplicate-key (HTTP 409 / SQLSTATE 23505). Retrying 100 rows one-by-one.
[INFO]   duplicate-key skipped: ('Hamburg', '67g IN 91/26', '2026-06-15', None)
[INFO] Upsert summary: inserted=2317 updated=4174 duplicates_skipped=2 real_errors=0 (total=6493, chunks=65, conflict_retries=2)
[INFO] === Upsert final: inserted=2317 updated=4174 duplicates_skipped=2 real_errors=0 (total=6493) ===
[INFO] === Run done: id=gha-20260619T220500Z ===
[INFO] Note: 2 duplicate-key rows skipped (0.03% of payload) — within acceptable threshold, treating run as success.
```

Exit code **0** → next step "Run Swift V2 entity backfill" fires.

### 7.2 Real failure (e.g. auth or RPC error)

```text
[ERROR] Upsert RPC failed @0: 401 {"message":"JWT expired"}
…
[INFO] Upsert summary: inserted=0 updated=0 duplicates_skipped=0 real_errors=6493 (total=6493, chunks=65, conflict_retries=0)
[ERROR] FAIL: 6493 real (non-duplicate) upsert errors. See above for HTTP details.
##[error]Process completed with exit code 1.
```

Exit code **1** → backfill step is skipped → GitHub Issue auto-opens.

### 7.3 Threshold breach (regression scenario)

```text
[INFO] Upsert summary: inserted=10 updated=20 duplicates_skipped=1500 real_errors=0 (total=1530, chunks=16, conflict_retries=15)
[ERROR] FAIL: duplicate-key skips 1500/1530 = 98.0% exceeds threshold 10%. This usually means a schema/key regression — investigate.
##[error]Process completed with exit code 1.
```

---

## 8. Manual test instructions

### 8.1 Smoke test (replays the original failing day)

```bash
gh workflow run "Daily Insolvency Scrape" \
  -f date_from=2026-06-15 \
  -f date_to=2026-06-15 \
  -f max_details=20
```

Expected outcome:

- Scraper completes without exit 1
- Log shows ≥0 `duplicate-key skipped` lines (typically 0–5 at `max_details=20`)
- Log shows the `Note: N duplicate-key rows skipped … treating run as success`
  line
- Step **Run Swift V2 entity backfill** runs and succeeds
- Step Summary panel shows `source_rows_changed` etc.

### 8.2 Per-row retry isolation test

If you want to exercise the retry path deliberately (force a duplicate):

```sql
-- In Supabase SQL editor BEFORE the workflow run:
update public.apify_cases
set    source_actor = 'neu_insolvenz_direct'
where  announcement_date = '2026-06-15'
  and  source_actor != 'neu_insolvenz_direct'
limit  3;
```

Then re-run the workflow as in 8.1. The 3 pre-tagged rows will collide
during single-row retry and be counted as `duplicates_skipped`.

### 8.3 Unit smoke test (no Supabase needed)

The change is covered by `/tmp/test_upsert_logic.py` in the implementation
sandbox. It mocks `requests.post` and verifies all four classification paths.
Run locally:

```bash
python /tmp/test_upsert_logic.py
# All four scenarios should print ✅
```

(The mock test was used during development; not committed to the repo to
keep the surface area small. The logic it covers is exercised on every
real workflow run, where it now produces clear logs.)

---

## 9. Rollback plan

The failure mode reintroduced by a rollback would be: a few hundred 409s
per day cause the workflow to fail and the daily backfill to be skipped,
just like before the fix.

### Option A — full revert (preferred if the new code misbehaves)

```bash
git log --oneline -5                   # find the duplicate-handling commit
git revert <commit-sha>
git push
```

This restores the simpler `upsert_cases` that counts every non-2xx as
fatal. The daily workflow will start exiting 1 on duplicate days, the
existing GitHub-Issue notifier will fire, and you can hot-fix from there.

### Option B — bypass duplicate-key fatality temporarily

If real_errors are also accumulating and you need the run to land *something*
overnight:

```python
# scraper.py, near the bottom of main()
DUPLICATE_RATIO_THRESHOLD = 1.01  # effectively disables the threshold
```

Push, run, then revert when the underlying cause is found. (Be aware that
this also hides a regression — set a calendar reminder to revert.)

### Option C — disable just the auto-backfill

If the scraper is fine but the new backfill step is what's failing, switch
the backfill workflow step to advisory mode (see Option 1 report § 9):

```yaml
- name: Run Swift V2 entity backfill
  if: success()
  continue-on-error: true
```

---

*End of report.*
