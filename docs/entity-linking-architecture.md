# Swift Assets V2 — Entity Linking Architecture

Date: 2026-06-18

This document describes the canonical data model used to connect data from three independent scrapers:

1. `neu.insolvenzbekanntmachungen.de` — official insolvency announcements
2. Handelsregister — company identity and registry events
3. Bundesanzeiger — published financial reports

The goal is to avoid scraper-to-scraper coupling. Each scraper writes to its own source table. A central entity layer links records that refer to the same company or natural person.

---

## Core principle

Scrapers do **not** talk to each other.

```text
source_neu_insolvenz_announcements       ┐
source_handelsregister_records           ├── entity_source_links ── portal_entities
source_bundesanzeiger_financials         ┘
```

`portal_entities` is the canonical identity table.

`entity_source_links` connects source records to the canonical entity.

---

## Golden company key

The strongest company match is:

```text
registry_identity_key = normalized_registry_court | registry_type | normalized_registry_number
```

Examples:

```text
koeln|HRB|123456
berlin|HRB|219214
muenchen|HRA|98765
```

If this key is present in two different sources, they are treated as the same company with confidence `100` and `match_method = registry_exact`.

---

## Fallback matching

If no registry data is available, we do **not** merge by name only.

Fallback entities are created with lower confidence using:

```text
entity_type | normalized_name | city | court | case_number
```

This is intentionally conservative. It prevents dangerous false merges such as two companies with the same or similar name.

---

## New database objects

Migration applied in Supabase under `swift_v2`.

### Canonical tables

- `swift_v2.portal_entities`
- `swift_v2.entity_source_links`

### Source tables

- `swift_v2.source_neu_insolvenz_announcements`
- `swift_v2.source_handelsregister_records`
- `swift_v2.source_bundesanzeiger_financials`

### Canonical business tables

- `swift_v2.registry_events`
- `swift_v2.financial_reports`
- `swift_v2.entity_match_candidates`

### Views / filters

- `swift_v2.v_portal_company_entities`
- `swift_v2.v_portal_natural_person_entities_active`
- `swift_v2.v_neu_company_leads`
- `swift_v2.v_neu_natural_person_active`
- `swift_v2.v_neu_companies_missing_registry`
- `swift_v2.v_handelsregister_enrichment_queue`
- `swift_v2.v_bundesanzeiger_financials_queue`
- `swift_v2.v_personal_data_retention_due`
- `swift_v2.v_entity_link_quality`

---

## Helper functions

### Normalization

- `swift_v2.norm_text(text)`
- `swift_v2.norm_key_part(text)`
- `swift_v2.norm_registry_type(text)`
- `swift_v2.norm_registry_number(text)`
- `swift_v2.registry_identity_key(court, type, number)`

### Classification

- `swift_v2.detect_legal_form(name)`
- `swift_v2.classify_subject_type(name, case_number, registry_type)`

Classification rules:

```text
registry_type present                 => company
company legal-form in name             => company
case number contains IK                => natural_person
name pattern like "Lastname, Firstname" => natural_person
otherwise                              => unknown
```

### Retention

- `swift_v2.entity_retention_until(entity_type, last_seen_at)`
- `swift_v2.delete_expired_personal_insolvency_data(dry_run boolean)`

Natural-person and unknown-sensitive records receive:

```text
retention_until = last_seen_at + 6 months
```

Companies do not receive this 6-month retention rule.

### Backfill / linking

- `swift_v2.backfill_neu_insolvenz_direct_entities(limit integer)`
- `swift_v2.upsert_entity_from_source(...)`

`upsert_entity_from_source(...)` is the generic function future scrapers should call after inserting raw records. It creates or updates `portal_entities` and inserts an `entity_source_links` row.

---

## Current backfill result

After applying the migration, the current official scraper data from `public.apify_cases` with `source_actor = 'neu_insolvenz_direct'` was copied and linked.

Result:

```text
source rows copied: 4165
source rows linked: 4165
source links:       4165
portal entities:    4162
```

Entity breakdown:

```text
company:        406 entities
natural_person: 3709 entities
unknown:        47 entities
```

Company linking quality:

```text
registry_exact:                401 links, confidence 100
official_insolvency_fallback:    7 links, confidence 75
```

Natural persons:

```text
natural_person: 3710 source links, confidence 80
retention_until: approximately 6 months after last_seen_at
```

Unknown-sensitive:

```text
unknown: 47 entities, needs_review, retention_until set
```

---

## Next scraper integration rules

### Handelsregister scraper

Write raw records to:

```text
swift_v2.source_handelsregister_records
```

For every source record, calculate:

```text
registry_identity_key = swift_v2.registry_identity_key(registry_court, registry_type, registry_number)
```

Then call:

```sql
select swift_v2.upsert_entity_from_source(
  p_source_name      => 'handelsregister_direct',
  p_source_table     => 'swift_v2.source_handelsregister_records',
  p_source_record_id => '<source_external_id_or_row_id>',
  p_display_name     => '<company_name>',
  p_city             => '<seat_city>',
  p_registry_court   => '<registry_court>',
  p_registry_type    => '<registry_type>',
  p_registry_number  => '<registry_number>',
  p_source_url       => '<source_url>',
  p_source_run_id    => '<run_id>',
  p_raw_payload      => '<raw_json>'::jsonb
);
```

If the registry key matches an existing insolvency entity, the Handelsregister row will link to the same company.

### Bundesanzeiger scraper

Write raw records to:

```text
swift_v2.source_bundesanzeiger_financials
```

Then call `swift_v2.upsert_entity_from_source(...)` with the company name, city, registry court/type/number if available.

After linking, insert normalized financial statements into:

```text
swift_v2.financial_reports
```

---

## Safety rules

1. Never merge two companies by name alone.
2. Exact registry match is the only automatic 100-confidence company match.
3. Fallback company matches without registry remain lower confidence.
4. Natural-person insolvency data is allowed but must respect the 6-month retention rule.
5. Unknown records are treated as sensitive until reviewed.
6. New scrapers should always store raw source data before normalization.
7. Production/API views should read from views, not directly from raw source tables.

---

## Immediate next steps

1. Modify the official insolvency scraper to write directly into `swift_v2.source_neu_insolvenz_announcements` or continue using `public.apify_cases` plus scheduled backfill.
2. Build Handelsregister scraper using `v_handelsregister_enrichment_queue`.
3. Build Bundesanzeiger scraper using `v_bundesanzeiger_financials_queue`.
4. Add a scheduled retention job that calls:

```sql
select * from swift_v2.delete_expired_personal_insolvency_data(false);
```

Only run deletion after reviewing `v_personal_data_retention_due`.
