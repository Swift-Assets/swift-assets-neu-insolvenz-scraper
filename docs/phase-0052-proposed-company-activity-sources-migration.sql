-- =============================================================================
-- PHASE 0052 — company_activity_sources (Gegenstand) enrichment storage (PROPOSED)
-- =============================================================================
-- Status: PROPOSED — NOT YET APPLIED.
--
-- IMPORTANT DISCREPANCY: the enrichment task brief stated that a prior migration
-- (0046) had already created swift_v2.company_activity_sources and the read view
-- swift_v2.v_company_activity_best, plus 4 activity columns on
-- v_cockpit_acquisition_inbox. As of 2026-06-29 NONE of these exist in project
-- hqyktreytsjeirlpnnyr (verified via information_schema). The enrichment worker
-- scripts/enrich_company_activity.py targets these objects; this migration creates
-- them so a real (non-dry) run can write. The worker tolerates the table being
-- absent in DRY_RUN (it treats "already enriched" as empty), so the dry run does
-- not depend on this migration — but the FIRST non-dry run does.
--
-- Apply via the same path as the other swift_v2 migrations (Supabase migration /
-- apply_migration), after review. The worker writes with the SERVICE ROLE key, so
-- no role grants are added here (matching the brief).
-- =============================================================================

create table if not exists swift_v2.company_activity_sources (
    id           bigint generated always as identity primary key,
    entity_id    uuid not null,
    -- 'unternehmensregister' (pass-2 official scrape) | 'aggregator' (snippet-derived)
    source       text not null check (source in ('unternehmensregister', 'aggregator')),
    activity_de  text,                 -- 1-2 sentence German summary of the Gegenstand
    activity_ar  text,                 -- faithful Arabic translation of activity_de
    purpose_raw  text,                 -- verbatim German Gegenstand the translation came from
    confidence   text not null check (confidence in ('high', 'medium')),
    source_ref   text,                 -- matched URL, or "<TYPE> <n> / <court>"
    matched_hrb  text,                 -- the registry number that satisfied the gate
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    constraint company_activity_sources_entity_source_uk unique (entity_id, source)
);

comment on table swift_v2.company_activity_sources is
    'Verified company business purpose (Gegenstand) in DE + AR. Filled by '
    'scripts/enrich_company_activity.py behind a strict HRB/court match gate. '
    'UPSERT key is (entity_id, source).';

create index if not exists company_activity_sources_entity_idx
    on swift_v2.company_activity_sources (entity_id);

-- Keep updated_at fresh on UPSERT (merge-duplicates issues an UPDATE).
create or replace function swift_v2.tg_company_activity_sources_touch()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists company_activity_sources_touch
    on swift_v2.company_activity_sources;
create trigger company_activity_sources_touch
    before update on swift_v2.company_activity_sources
    for each row execute function swift_v2.tg_company_activity_sources_touch();

-- Best row per company: prefer the official unternehmensregister scrape over a
-- snippet-derived aggregator row, and 'high' confidence over 'medium'.
create or replace view swift_v2.v_company_activity_best as
select distinct on (entity_id)
       entity_id, source, activity_de, activity_ar, purpose_raw,
       confidence, source_ref, matched_hrb, created_at, updated_at
from swift_v2.company_activity_sources
order by entity_id,
         (source = 'unternehmensregister') desc,
         (confidence = 'high') desc,
         updated_at desc;

comment on view swift_v2.v_company_activity_best is
    'One best verified activity row per entity_id for the cockpit to read.';
