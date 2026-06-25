# PHASE 0044B — Admin parser migration + NULL-only backfill (results)

Applied to project `hqyktreytsjeirlpnnyr`, schema `swift_v2`, on approval
"موافق على 0044B". Source: `docs/phase-0044-proposed-admin-parser-migration.sql`
Section 1 (function) + Section 2 (NULL-only backfill). Section 3 NOT applied.

## What was applied
1. **Function (Section 1):** `CREATE OR REPLACE FUNCTION
   swift_v2.fn_parse_insolvency_admin(text)` — migration
   `phase_0044b_decouple_admin_contact_parser`. Decouples a labelled
   `E-Mail:`/`Tel.:` contact from name extraction: when no administrator NAME
   can be anchored but a contact is present AND an administrator role term
   (Insolvenzverwalter/Sachwalter/Treuhänder) appears, the contact is returned;
   name/firm/address stay NULL (an address is never returned without a name
   anchor). Otherwise byte-identical to the prior function. IMMUTABLE, additive.
2. **Backfill (Section 2):** NULL-only, keyset-paginated (by `id`), 500/batch on
   `swift_v2.source_neu_insolvenz_announcements`, setting only `updated_at=now()`
   to re-fire the existing COALESCE-only trigger `trg_fill_insolvency_admin`.

Section 3 (apify_cases columns / RPC) was **NOT** applied (out of scope).

## Safety
- Trigger `trg_fill_insolvency_admin` confirmed COALESCE-only (never overwrites
  non-null). Backfill SQL set no admin column directly — only `updated_at`.
- No DELETE/TRUNCATE/DROP/destructive ALTER. No `public.apify_cases` writes. No
  RLS/grant changes. No portal changes. No scraper/workflow run. No raw
  announcement text or debtor names printed. No secrets.

## Backfill execution
| batch | rows touched |
|---|---:|
| 1 | 500 |
| 2 | 500 |
| 3 | 500 |
| 4 | 269 |
| 5 (confirm) | 0 |
| **total** | **1,769** |

Stopped because the candidate set was exhausted (final batch = 0), well under the
10-batch / 5,000-row cap. Post-apply candidate count was 1,769 (< 2,000 gate).

## Before / after (swift_v2.source_neu_insolvenz_announcements, counts only)
| field | before | after | gained |
|---|---:|---:|---:|
| insolvency_administrator (name) | 3,445 | 3,445 | +0 |
| insolvency_admin_firm | 129 | 129 | +0 |
| insolvency_admin_address | 1,173 | 1,173 | +0 |
| insolvency_admin_phone | 1,046 | **1,500** | **+454** |
| insolvency_admin_email | 506 | **825** | **+319** |

Matches the Phase 0044 read-only estimate (~319 emails, ~454 phones) exactly.
name/firm/address unchanged by design — the decoupled fallback only adds
email/phone, never a name/firm/address without a trigger anchor.

## No-overwrite validation
- Method: monotonic count check + COALESCE-only trigger + backfill writes only
  `updated_at`. Result: no admin-field count decreased; phone/email increased,
  name/firm/address unchanged. Overwrite is impossible by construction.

## Cockpit-facing note
`v_cockpit_acquisition_inbox` is built from the per-user watchlist and references
`auth.uid()`; in a service-role SQL session it returns 0 rows, so cockpit-facing
admin coverage cannot be measured here. Its `administrator_email/phone/address`
columns read from the swift columns improved above, so the recovered contacts
surface for authenticated cockpit users with matching watchlist items.

## Remaining
- Merge the Phase 0044 code branch to `main` (scraper-side parity + workflow
  fixes are still only on `claude/dreamy-meitner-o2dp02`).
- Cockpit UI patch to display administrator address + Kanzlei/firm
  (`Swift-Assets/swift-assets-cockpit`).
- Optional apify_cases subfield persistence/RPC hygiene (Section 3).
