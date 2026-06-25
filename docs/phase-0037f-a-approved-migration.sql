-- =============================================================================
-- PHASE 0037F-A — APPROVED migration (existing-text recovery only)
-- =============================================================================
-- User approval: "موافق على 0037F-A". Scope is STRICTLY the safe Part A from
-- Phase 0037E: recover announcement_type_hint for rows that ALREADY have
-- announcement_text in the database. NO portal scraping, NO detail re-fetch,
-- NO writes to text-empty rows.
--
-- This migration is split into two parts that were applied separately and
-- verified by read-back (see docs/phase-0037f-a-report — chat):
--   Part 1 (DDL, additive): extend the cockpit phase mapping so the recovered
--           late-stage/administrative types surface as 'monitor' instead of
--           'unknown'. Purely additive WHEN branches; no existing branch or
--           output changes; no data rows touched.
--   Part 2 (DML, guarded): set announcement_type_hint on the text-bearing
--           NULL rows in public.apify_cases and mirror to swift_v2. Every
--           statement is guarded to text-bearing + derived-non-null + source
--           'neu_insolvenz_direct', so text-empty rows are never touched.
--
-- The SQL CASE below is an EXACT mirror of the Phase 0037D/0037F-A Python
-- classifier scraper.TYPE_KEYWORDS (umlaut-folded, same priority order, the
-- 0037F-A keywords appended at lowest priority). Measured candidate count: 493
-- (< 600 cap). No destructive SQL (no DROP/TRUNCATE/DELETE), no RLS change.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- PART 1 — additive cockpit phase mapping (DDL)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION swift_v2.fn_cockpit_phase_label(p_announcement_type text, p_phase_hint text DEFAULT NULL::text)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE
 SET search_path TO 'swift_v2', 'public', 'pg_catalog'
AS $function$
  select case
    when coalesce(p_announcement_type,'') ~* '(vorläufig|vorlaeufig|anordnung|sicherungsma)' then 'vorlaeufig'
    when coalesce(p_announcement_type,'') ~* '(eröffnung|eroeffnung|eröffnet|eroeffnet)' then 'eroeffnung'
    when coalesce(p_announcement_type,'') ~* 'berichtstermin' then 'berichtstermin'
    when coalesce(p_announcement_type,'') ~* '(prüfungstermin|pruefungstermin)' then 'pruefungstermin'
    when coalesce(p_announcement_type,'') ~* '(verwertung|masseverwertung)' then 'verwertung'
    when coalesce(p_announcement_type,'') ~* '(schlussverteilung|schlusstermin)' then 'schlussverteilung'
    when coalesce(p_announcement_type,'') ~* 'verteilung' then 'verteilung'
    when coalesce(p_announcement_type,'') ~* 'aufhebung' then 'aufhebung'
    when coalesce(p_announcement_type,'') ~* '(einstellung|mangels masse|masseunzulänglich|masseunzulaenglich)' then 'einstellung_mangels_masse'
    when coalesce(p_announcement_type,'') ~* 'restschuldbefreiung' then 'restschuldbefreiung'
    when coalesce(p_announcement_type,'') ~* '(vergütungsfestsetzung|verguetungsfestsetzung|vergütung|verguetung)' then 'verguetungsfestsetzung'
    -- NEW (0037F-A): small high-confidence late-stage/administrative types that
    -- previously fell through to 'unknown'. All -> 'monitor' (track only).
    when coalesce(p_announcement_type,'') ~* 'insolvenzplan' then 'insolvenzplan'
    when coalesce(p_announcement_type,'') ~* 'schlussrechnung' then 'schlussrechnung'
    when coalesce(p_announcement_type,'') ~* '(gläubigerversammlung|glaeubigerversammlung|gläubigerausschuss|glaeubigerausschuss)' then 'glaeubigerversammlung'
    when coalesce(p_announcement_type,'') ~* '(treuhänder|treuhaender)' then 'treuhaender'
    when coalesce(p_announcement_type,'') ~* '(verfahrenskostenstundung|stundung)' then 'stundung'
    when coalesce(p_announcement_type,'') ~* 'bestellung' then 'bestellung'
    -- fall back to coarse phase hint (v_entity_insolvency_phase.phase)
    when p_phase_hint = 'preliminary_administration' then 'vorlaeufig'
    when p_phase_hint = 'opening' then 'eroeffnung'
    when p_phase_hint = 'administrator_appointed' then 'eroeffnung'
    when p_phase_hint = 'late_stage' then 'verteilung'
    when p_phase_hint = 'masseunzulaenglichkeit' then 'einstellung_mangels_masse'
    else 'unknown'
  end;
$function$;

CREATE OR REPLACE FUNCTION swift_v2.fn_cockpit_phase_priority(p_announcement_type text, p_document_text text DEFAULT NULL::text)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE
 SET search_path TO 'swift_v2', 'public', 'pg_catalog'
AS $function$
  select case swift_v2.fn_cockpit_phase_label(p_announcement_type, p_document_text)
    when 'vorlaeufig' then 'high'
    when 'eroeffnung' then 'high'
    when 'berichtstermin' then 'high'
    when 'pruefungstermin' then 'high'
    when 'verwertung' then 'high'
    when 'verteilung' then 'low'
    when 'schlussverteilung' then 'monitor'
    when 'aufhebung' then 'monitor'
    when 'einstellung_mangels_masse' then 'monitor'
    when 'restschuldbefreiung' then 'monitor'
    when 'verguetungsfestsetzung' then 'monitor'
    -- NEW (0037F-A): late-stage / administrative -> track only (never high/low).
    when 'insolvenzplan' then 'monitor'
    when 'schlussrechnung' then 'monitor'
    when 'glaeubigerversammlung' then 'monitor'
    when 'treuhaender' then 'monitor'
    when 'stundung' then 'monitor'
    when 'bestellung' then 'monitor'
    else 'unknown'
  end;
$function$;

-- ---------------------------------------------------------------------------
-- PART 2 — guarded recovery (DML). Text-bearing rows ONLY.
-- ---------------------------------------------------------------------------
-- 2a) Set the derived type on public.apify_cases (source of truth).
with derived as (
  select ac.id,
    case
     when f like '%abweisung mangels masse%' then 'Abweisung mangels Masse'
     when f like '%einstellung mangels masse%' then 'Einstellung mangels Masse'
     when f like '%masseunzulaenglichkeit%' then 'Masseunzulänglichkeit'
     when f like '%restschuldbefreiung%' then 'Restschuldbefreiung'
     when f like '%verguetungsfestsetzung%' or f like '%verguetung%' then 'Vergütungsfestsetzung'
     when f like '%anordnung der vorlaeufigen%' or f like '%vorlaeufige insolvenzverwalt%' or f like '%vorlaeufiger insolvenzverwalt%' or f like '%sicherungsmassnahme%' then 'Sicherungsmaßnahmen'
     when f like '%schlussverteilung%' or f like '%schlusstermin%' then 'Schlussverteilung'
     when f like '%masseverwertung%' or f like '%verwertung%' then 'Verwertung'
     when f like '%verteilungsverzeichnis%' or f like '%verteilung%' then 'Verteilung'
     when f like '%berichtstermin%' then 'Berichtstermin'
     when f like '%pruefungstermin%' then 'Prüfungstermin'
     when f like '%aufhebung%' or f like '%aufgehoben%' then 'Aufhebung'
     when f like '%eroeffnung%' or f like '%eroeffnet%' then 'Eröffnung'
     when f like '%einstellung%' or f like '%eingestellt%' then 'Einstellung'
     when f like '%anordnung%' or f like '%angeordnet%' then 'Anordnung'
     when f like '%bestellung%' or f like '%bestellt%' then 'Bestellung'
     when f like '%insolvenzplan%' then 'Insolvenzplan'
     when f like '%schlussrechnung%' then 'Schlussrechnung'
     when f like '%glaeubigerversammlung%' or f like '%glaeubigerausschuss%' then 'Gläubigerversammlung'
     when f like '%treuhaender%' then 'Treuhänder'
     when f like '%verfahrenskostenstundung%' or f like '%stundung%' then 'Verfahrenskostenstundung'
     else null end as derived_type
  from public.apify_cases ac
  cross join lateral (select regexp_replace(replace(replace(replace(replace(
     lower(btrim(ac.announcement_text)),'ä','ae'),'ö','oe'),'ü','ue'),'ß','ss'),'\s+',' ','g') as f) z
  where ac.source_actor = 'neu_insolvenz_direct'
    and ac.announcement_type_hint is null
    and nullif(btrim(ac.announcement_text),'') is not null
)
update public.apify_cases ac
set announcement_type_hint = d.derived_type, updated_at = now()
from derived d
where ac.id = d.id and d.derived_type is not null;

-- 2b) Mirror the recovered type onto swift_v2 (only rows still NULL there whose
--     apify row now carries a type and has text). Never touches text-empty rows.
update swift_v2.source_neu_insolvenz_announcements s
set announcement_type_hint = ac.announcement_type_hint, updated_at = now()
from public.apify_cases ac
where s.source_apify_case_id = ac.id
  and s.announcement_type_hint is null
  and ac.announcement_type_hint is not null
  and nullif(btrim(ac.announcement_text),'') is not null;
-- =============================================================================
