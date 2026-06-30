-- =============================================================================
-- PHASE 0053 — company_activity_attempts (backfill progress ledger) (PROPOSED)
-- =============================================================================
-- Status: PROPOSED — NOT YET APPLIED.
--
-- WHY: scripts/enrich_company_activity.py selected "eligible companies with NO
-- row in company_activity_sources", ordered entity_id.asc. Rejected companies
-- never get a sources row, so every batch re-selected the SAME head of
-- hard/unrecoverable companies forever — credits spent (~815/run), zero advance.
--
-- This ledger records EVERY processed company (accepted OR rejected). Selection
-- then excludes any entity whose last_attempt_at is within RETRY_AFTER_DAYS
-- (default 30), so each batch ADVANCES to new companies instead of looping on the
-- same head. Idempotent: UPSERT key is entity_id.
--
-- The worker writes with the SERVICE ROLE key, so no role grants are added here
-- (matching the other swift_v2 migrations). The worker TOLERATES this table being
-- absent — it then treats the ledger as empty and SKIPS writing attempts, logging
-- that the backfill will not advance until this migration is applied. DRY_RUN does
-- not depend on it; the first advancing non-dry run does.
--
-- Apply via the same path as the other swift_v2 migrations (Supabase migration /
-- apply_migration), after review.
-- =============================================================================

create table if not exists swift_v2.company_activity_attempts (
    entity_id        uuid primary key,
    attempts         integer not null default 0,    -- times this company was processed
    last_attempt_at  timestamptz not null default now(),
    last_reason      text,                           -- Decision.reason of the last attempt
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

comment on table swift_v2.company_activity_attempts is
    'Backfill progress ledger for scripts/enrich_company_activity.py: one row per '
    'processed company (accepted OR rejected) so candidate selection skips '
    'recently-attempted entities and advances past the unrecoverable head. '
    'UPSERT key is entity_id.';

-- Selection filters on last_attempt_at (the RETRY_AFTER_DAYS window).
create index if not exists company_activity_attempts_last_attempt_idx
    on swift_v2.company_activity_attempts (last_attempt_at);

-- Keep updated_at fresh on UPSERT (merge-duplicates issues an UPDATE).
create or replace function swift_v2.tg_company_activity_attempts_touch()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists company_activity_attempts_touch
    on swift_v2.company_activity_attempts;
create trigger company_activity_attempts_touch
    before update on swift_v2.company_activity_attempts
    for each row execute function swift_v2.tg_company_activity_attempts_touch();
