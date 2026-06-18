# Option 1 — Backfill Automation Report

**Date:** 2026-06-18
**Repo:** `Swift-Assets/swift-assets-neu-insolvenz-scraper`
**Status:** Implemented · temporary bridge until the scraper writes directly to `swift_v2`

---

## 1. Summary

The daily insolvency scraper continues to write to `public.apify_cases` exactly as
before. We added a second step to the GitHub Actions workflow that runs
immediately after a successful scraper run and calls the existing Supabase RPC:

```text
swift_v2.backfill_neu_insolvenz_direct_entities(null)
```

This RPC copies the new `source_actor = 'neu_insolvenz_direct'` rows into the
Swift V2 entity architecture (`source_neu_insolvenz_announcements`,
`portal_entities`, `entity_source_links`) and links them to canonical entities.

No database objects were changed. No secrets were exposed. The existing
scraper is preserved exactly.

---

## 2. Files changed

| File | Type | Purpose |
|---|---|---|
| `scripts/run_swift_v2_backfill.py` | **new** | Calls the RPC over PostgREST using only env-var secrets, prints a safe summary, exits non-zero on failure |
| `.github/workflows/daily-scrape.yml` | modified | Adds a new step "Run Swift V2 entity backfill" after the scraper step |
| `docs/option-1-backfill-automation-report.md` | **new** | This report |

The scraper itself (`scraper.py`), the schema (`apify_cases`, `swift_v2.*`),
and the daily cron schedule are **not** changed.

---

## 3. New GitHub Actions flow

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Trigger: cron "5 22 * * *" UTC  (= 00:05 Berlin)  OR  manual run     │
└──────────────────────────────────────────────────────────────────────┘
        │
        ▼
   Checkout
        │
        ▼
   Set up Python 3.12  +  pip install -r requirements.txt
        │
        ▼
   Run scraper                ← python scraper.py
        │                        writes to public.apify_cases
        │                        source_actor = 'neu_insolvenz_direct'
        │
        ▼
   Run Swift V2 entity backfill  (NEW · only if scraper succeeded)
        │                        python scripts/run_swift_v2_backfill.py
        │                        → POST /rest/v1/rpc/backfill_neu_insolvenz_direct_entities
        │                          Accept-Profile: swift_v2
        │                          Content-Profile: swift_v2
        │                          body: {"p_limit": null}
        │
        ▼
   Workflow result
   - any step failed → GitHub Issue auto-created (existing failure-notifier step)
   - all steps green → daily run complete
```

Step ordering and `if: success()` guarantee the backfill never runs when the
scraper failed.

---

## 4. Exact Supabase RPC being called

```text
POST  {SUPABASE_URL}/rest/v1/rpc/backfill_neu_insolvenz_direct_entities

Headers:
  apikey:           ${SUPABASE_SERVICE_ROLE_KEY}
  Authorization:    Bearer ${SUPABASE_SERVICE_ROLE_KEY}
  Content-Type:     application/json
  Accept:           application/json
  Accept-Profile:   swift_v2
  Content-Profile:  swift_v2
  Prefer:           return=representation

Body:
  {"p_limit": null}
```

The RPC's SQL signature (verified in the database):

```sql
swift_v2.backfill_neu_insolvenz_direct_entities(p_limit integer DEFAULT NULL)
RETURNS TABLE(
  source_rows_changed integer,
  source_rows_total   integer,
  entities_total      integer,
  source_rows_linked  integer,
  links_total         integer
)
```

PostgREST exposes non-`public` schemas via the `Accept-Profile` / `Content-Profile`
headers (the RFC 7240 profile-negotiation convention). **Prerequisite:** the
schema `swift_v2` must be listed under *Project Settings → API → Exposed
schemas* in Supabase. If it is not, the call returns HTTP 404 with
`schema "swift_v2" not exposed` — add `swift_v2` there, redeploy is not
needed.

---

## 5. Safety review

| Concern | Status | Notes |
|---|---|---|
| Plaintext secrets in code | ✅ none | Script reads `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` only from `os.environ` |
| Service-role key printed/logged | ✅ never | `_redact()` strips any accidental occurrence from error strings; success path prints only result counts |
| `.env` file committed | ✅ no | `.gitignore` already excludes `.env*` |
| JWT exposure | ✅ none | Headers are set on the in-process `urllib` request, never echoed |
| Old Make credentials | ✅ untouched | Workflow only reads existing GitHub Actions secrets |
| Schema rewrite | ✅ none | No DDL of any kind |
| Existing scraper preserved | ✅ yes | `scraper.py` and its single "Run scraper" step are unchanged |
| Backfill runs only after scraper success | ✅ yes | `if: success()` guard on the new step (also implicit step order) |
| Daily schedule changed | ✅ no | Cron remains `5 22 * * *` UTC |
| Failure surfaces clearly | ✅ yes | Non-2xx HTTP exits with code 4; the existing GitHub-Issue notifier fires on any failed step |

The script uses only the Python standard library (`urllib`, `json`) so no new
third-party dependency is introduced.

---

## 6. Expected data flow

```text
                        ┌──────────────────────────────┐
       neu.insolvenz   │       scraper.py             │
       bekanntmach.de  │   (public.apify_cases)       │
       ───────────►    │  source_actor=               │
                        │  'neu_insolvenz_direct'      │
                        └──────────────┬───────────────┘
                                       │  (1) insert / fill-only upsert
                                       ▼
                ┌─────────────────────────────────────────────┐
                │      public.apify_cases                     │
                │  (existing canonical raw table)             │
                └──────────────┬──────────────────────────────┘
                               │  (2) RPC swift_v2.backfill_
                               │        neu_insolvenz_direct_entities(null)
                               ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │                          swift_v2                                  │
   │                                                                    │
   │   source_neu_insolvenz_announcements   ← raw, per-source mirror    │
   │                  │                                                 │
   │                  │   linked via entity_source_links                │
   │                  ▼                                                 │
   │           portal_entities             ← canonical entity table     │
   │            (companies are matched                                  │
   │             by registry_identity_key)                              │
   └────────────────────────────────────────────────────────────────────┘
```

- `portal_entities` is the **canonical** entity table.
- `entity_source_links` connects each source row to its canonical entity.
- **Companies** are linked by `registry_identity_key` (court + register type + register number).
- **Natural-person insolvency** data is subject to a **6-month retention** policy
  (handled inside the Swift V2 layer, not in this scraper).

---

## 7. How to test manually from GitHub Actions

1. Open the repo on GitHub → **Actions** tab.
2. Select **Daily Insolvency Scrape** in the left sidebar.
3. Click **Run workflow** (top right).
4. (Optional) Fill the inputs:
   - `date_from`, `date_to` — leave empty to use today; or set both to e.g. `2026-06-15` to re-process a known-good day.
   - `max_details` — e.g. `20` for a fast smoke test.
5. Click the green **Run workflow** button.
6. Click into the run → expand the step **Run Swift V2 entity backfill**.

Alternatively, from any host with `gh` configured:

```bash
gh workflow run "Daily Insolvency Scrape" \
  -f date_from=2026-06-15 \
  -f date_to=2026-06-15 \
  -f max_details=20
```

---

## 8. What log output to expect

Successful run, step **Run Swift V2 entity backfill**:

```text
[run_swift_v2_backfill] Calling RPC: swift_v2.backfill_neu_insolvenz_direct_entities(null)
[run_swift_v2_backfill] Endpoint:   https://hqyktreytsjeirlpnnyr.supabase.co/rest/v1/rpc/backfill_neu_insolvenz_direct_entities
[run_swift_v2_backfill] Profile:    swift_v2
[run_swift_v2_backfill] ✅ Backfill RPC succeeded
[run_swift_v2_backfill] Result summary:
  - source_rows_changed     : 4165
  - source_rows_total       : 4165
  - entities_total          : 4162
  - source_rows_linked      : 4165
  - links_total             : 4165
```

The same five counters are also appended to the GitHub Actions **Step Summary**
panel as a small Markdown table, so they are visible without expanding the log.

Failure modes:

| Symptom | Exit | Likely cause |
|---|---|---|
| `SUPABASE_URL is not set` | 2 | Workflow secret missing or step env block broken |
| `HTTP transport error` | 3 | Supabase unreachable / DNS / network |
| `RPC returned HTTP 404 …schema "swift_v2" not exposed` | 4 | Add `swift_v2` to Project Settings → API → Exposed schemas |
| `RPC returned HTTP 401` | 4 | Service-role key invalid / rotated |
| `RPC returned HTTP 5xx` | 4 | Postgres function error — read the truncated body in the log |

Any non-zero exit fails the workflow, which fires the existing GitHub-Issue
notifier with a link to the run.

---

## 9. Rollback plan

If the backfill step misbehaves and we need to revert to "scraper only, no
backfill from CI":

**Option A — disable just the new step (fastest, no commit needed):**

1. GitHub → **Actions** → **Daily Insolvency Scrape** → ⋯ → **Disable workflow**
   then re-enable a copy without the new step. *(Heavy.)*

**Option B — comment out the step (one-line revert):**

```bash
git revert <commit-sha-of-this-change>
git push
```

This restores `daily-scrape.yml` and removes `scripts/run_swift_v2_backfill.py`.
The scraper continues to run on schedule and writes to `public.apify_cases`
exactly as before. The RPC can still be invoked manually from the Supabase SQL
editor:

```sql
select * from swift_v2.backfill_neu_insolvenz_direct_entities(null);
```

**Option C — switch the step from blocking to advisory:**

Change `if: success()` to keep the step but append `continue-on-error: true`,
so a backfill failure no longer fails the workflow. Useful while debugging.

---

## 10. Open risks and next improvements

**Open risks**

- `swift_v2` must remain in *Exposed schemas* in the Supabase API config — if a
  teammate removes it, the new step will start failing with HTTP 404. The
  failure is loud and rolls back cleanly, but it would block the daily run.
- The RPC currently operates on **all** unlinked `neu_insolvenz_direct` rows
  (`p_limit = null`). If the table grows to millions of unmatched rows the
  call could exceed the 600 s urllib timeout. Mitigation listed below.
- Service-role key is shared with the scraper. Rotating it requires updating
  exactly one GitHub Actions secret (`SUPABASE_SERVICE_ROLE_KEY`).

**Next improvements (in priority order)**

1. **Option 2 — make the scraper write directly to `swift_v2.source_neu_insolvenz_announcements`** and remove the backfill bridge. This is the original architectural target; today's setup is explicitly a temporary bridge.
2. **Time-bounded backfill:** pass a sensible `p_limit` (e.g. `2 * MAX_DETAIL_FETCHES`) so the call always runs in a few seconds even after long outages.
3. **Per-run delta query:** instead of a global backfill, link only the rows produced by the current scraper run via `source_run_id`. Requires a small RPC variant.
4. **Step-summary enrichment:** include per-day deltas (entities created, links created today) in the Markdown summary so the daily run produces a one-glance report.
5. **Alerting on stuck rows:** add a daily query that counts `neu_insolvenz_direct` rows in `public.apify_cases` without a matching `entity_source_links` entry and opens an issue if it exceeds a threshold.

---

*End of report.*
