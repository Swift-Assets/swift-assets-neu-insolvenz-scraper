# PHASE 0037E — Dry-run re-ingestion analysis (NO production writes)

Read-only analysis of the existing rows where
`swift_v2.source_neu_insolvenz_announcements.announcement_type_hint IS NULL`.
No production `UPDATE/INSERT/DELETE`, no migration applied, no backfill, no
portal scrape, no AI. All figures from `SELECT`-only queries; the classifier
estimate was computed **server-side in SQL** (a faithful replica of the
Phase 0037D `detect_announcement_type` folded-keyword set + priority order) so
**no announcement text ever left the database** (privacy: counts only).

Snapshot date: 2026-06-25. Project `hqyktreytsjeirlpnnyr`, schema `swift_v2`.

> ⚠️ Existing NULL rows are **NOT** fixed by this phase. This is analysis +
> an approval-gated plan only.

## 1. Breakdown of the 10,926 NULL-type rows

| group | count | share |
|---|---:|---:|
| **NULL `announcement_type_hint` total** | **10,926** | 100% |
| → has non-empty `apify_cases.announcement_text` (re-classify offline) | **1,060** | 9.7% |
| → text-empty (needs detail re-fetch) | **9,866** | 90.3% |

By `subject_type`:

| subject_type | total | has_text | text_empty |
|---|---:|---:|---:|
| natural_person | 9,618 | 834 | 8,784 |
| company | 1,169 | 188 | 981 |
| unknown | 139 | 38 | 101 |

Text-empty rows by `detail_fetch_attempts`:

| attempts | count |
|---|---:|
| 0 (never attempted) | 9,416 |
| 1–4 | 450 |
| 5+ (capped / dropped) | **0** |

All 9,866 text-empty rows fall on **8 distinct `announcement_date`s**
(2026-06-15 → 2026-06-25), all within the last 30 days, and **none are capped**
— so every one is still reachable with a per-day re-search.

## 2. Offline classifier simulation (1,060 text-bearing rows)

Applying the Phase 0037D classifier (SQL replica) to the rows that already have
text:

| outcome | count | share |
|---|---:|---:|
| would classify to a type | **433** | 40.8% |
| stays NULL (no recognizable type) | 627 | 59.2% |

Distribution of the 433 that classify: Vergütungsfestsetzung 145, Verteilung
143, Bestellung 74, Masseunzulänglichkeit 70, Verwertung 1.

Probe of the 627 still-unclassified (counts only) → remaining classifier gaps
are **small**: Gläubigerversammlung/-ausschuss 52, Insolvenzplan 2, Stundung 2,
Treuhänder 3, Schlussrechnung 1. The rest are generic notices (`Beschluss` 96,
generic `Termin` 61) that carry **no single announcement type** and should stay
NULL (do-not-fabricate). A targeted classifier extension (Gläubigerversammlung,
Insolvenzplan, Verfahrenskostenstundung, Treuhänder-Bestellung) would recover
roughly **+55–60** rows → ~46% of the text-bearing set; the remainder is
legitimately untyped.

## 3. Approval-gated recovery plan for Phase 0037F

**Part A — re-classify from EXISTING text (no portal access):**
1. (Optional, recommended) extend `TYPE_KEYWORDS` with the gap keywords above
   (+~55–60 rows) — code change, reviewed.
2. Apply `docs/phase-0037d-proposed-migration.sql` (RPC + sync) so the type
   produced from existing `announcement_text` is written and flows to swift_v2.
3. Run `swift_v2.backfill_neu_insolvenz_direct_entities(p_limit)` (its existing
   re-sync predicate already fires on `ac.announcement_type_hint is not null and
   s.announcement_type_hint is null`). **Expected: ~433 (→ ~490 with the
   classifier extension) rows gain a type with zero portal traffic.**

**Part B — targeted detail re-fetch (9,866 text-empty rows, 8 days):**
1. Run the scraper in its normal backfill mode scoped to 2026-06-15…2026-06-25
   with a raised `MAX_DETAIL_FETCHES` (≥10,000) so the reserved backfill budget
   covers all 9,866 in one or two runs. None are capped, so all are candidates.
2. As text arrives, the hardened classifier sets `announcement_type_hint`,
   `insolvency_phase`, admin fields, etc., and the sync carries them to swift_v2.
3. Rows that still return empty text after `MAX_DETAIL_ATTEMPTS` legitimately
   stay NULL (source provides nothing).

**Exact artifacts to review before 0037F:**
- `docs/phase-0037d-proposed-migration.sql` (RPC + sync extension; not applied).
- (Optional) classifier-extension diff to `TYPE_KEYWORDS` in `scraper.py`.
- A scoped, approved scraper run config (date window + raised budget) — **no
  unattended production write until approved.**

## 4. What is NOT fixed

The 10,926 existing NULL rows remain NULL. ~9,866 cannot be classified until
their detail text is re-fetched; ~627 text-bearing rows are genuinely untyped.
Production recovery requires the approved Phase 0037F steps above.
