-- =============================================================================
-- PHASE 0044 — PROPOSED migration (DO NOT APPLY without explicit approval)
-- =============================================================================
-- Goal: improve Insolvenzverwalter contact extraction COVERAGE for the Cockpit.
--
-- Diagnosis (read-only, Phase 0044):
--   * The Cockpit reads administrator_name/email/phone/address from
--     swift_v2.v_cockpit_acquisition_inbox, which is fed by the swift columns
--     filled by trigger trg_fill_insolvency_admin -> swift_v2.fn_parse_insolvency_admin.
--   * That DB parser computes v_email / v_phone FIRST, but then does
--     `IF cand IS NULL THEN RETURN '{}'` — i.e. it THROWS AWAY a labelled
--     E-Mail:/Tel.: contact whenever the administrator NAME block cannot be
--     anchored. On a 4,000-row trigger-text sample only 22.9% yielded a name,
--     so the majority of contacts were discarded. Measured immediately
--     recoverable by decoupling: ~349 emails + ~880 phones (exact-regex count).
--
-- Fix (this migration): mirror the Phase 0044 Python change
-- (scraper.parse_insolvency_admin) — when no NAME can be anchored but a labelled
-- email/phone exists AND an administrator role is mentioned, return that contact
-- (the administrator's office). name/firm/address stay NULL (never guessed; an
-- address is never returned without a name anchor, to avoid capturing the
-- debtor's address). Logic is otherwise byte-identical to the current function.
--
-- The trigger trg_fill_insolvency_admin is UNCHANGED: it already COALESCE-fills
-- only NULL columns (never overwrites verified values), so applying this safely
-- improves future inserts/updates; existing rows are filled by the NULL-only
-- backfill in section 2 (also approval-gated).
--
-- Safety: CREATE OR REPLACE of an IMMUTABLE function. No data mutation, no DROP,
-- no schema change, no RLS change. Reversible by re-applying the prior body.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Decoupled DB parser (parity with Python parse_insolvency_admin)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION swift_v2.fn_parse_insolvency_admin(p_text text)
 RETURNS jsonb
 LANGUAGE plpgsql
 IMMUTABLE
AS $function$
DECLARE
  t text; cand text; tail text; m text[];
  v_name text; v_firm text; v_addr text; v_phone text; v_email text; v_street text; v_plz text;
  v_contact jsonb;                      -- NEW (0044): contact-only fallback
  has_title boolean;
  street_re constant text :=
    '(?:[A-ZÄÖÜ][A-Za-zäöüß.\-]*(?:[Ss]tra(?:ß|ss)e|[Ss]tr\.|[Aa]llee|[Ww]eg|[Pp]latz|[Rr]ing|[Gg]asse|[Dd]amm|[Uu]fer|[Ww]all|[Cc]haussee|[Ll]andstr\.?)'
    || '|[A-ZÄÖÜ][A-Za-zäöüß.\-]+ (?:Stra(?:ß|ss)e|Str\.|Allee|Weg|Platz|Ring|Gasse|Damm|Ufer|Wall|Chaussee))\.? \d+ ?[a-z]?';
  title_re constant text := '^(?:Rechtsanw[äa]lt(?:in)?|RA(?:in)?\.?|Dr\.|Prof\.|Dipl\.\-?\w*|Herr|Frau)';
  stop_re constant text := '(?: Die (?:Insolvenz)?[Gg]läubiger| Insolvenzforderungen sind| Forderungen sind| §| \d\.? Die| bestellt| ernannt| bestimmt)';
BEGIN
  IF p_text IS NULL OR btrim(p_text) = '' THEN RETURN '{}'::jsonb; END IF;
  t := regexp_replace(p_text, '\s+', ' ', 'g');
  v_email := (regexp_match(t, 'E-?Mail\s*:?\s*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', 'i'))[1];
  v_phone := btrim((regexp_match(t, 'Tel(?:efon)?\.?\s*:?\s*(\+?\d[\d /().\-]{4,}\d)', 'i'))[1]);

  -- NEW (0044): contact-only fallback used at every name-failure exit below.
  IF (v_email IS NOT NULL OR v_phone IS NOT NULL)
     AND p_text ~* '(Insolvenzverwalter|Sachwalter|Treuh(ä|ae)nder)' THEN
    v_contact := jsonb_strip_nulls(jsonb_build_object('phone', v_phone, 'email', v_email));
  ELSE
    v_contact := '{}'::jsonb;
  END IF;

  cand := (regexp_match(t,
    '(?i)(?:zum |zur |zu )?(?:vorl[äa]ufige[rn]? )?(?:Insolvenzverwalter(?:in)?|Sachwalter(?:in)?|Treuh[äa]nder(?:in)?) '
    || '(?:(?:ist|wird|hiermit|bestellt|bestimmt|ernannt|worden|zum|zur)[\s:]+){1,5}(.{3,255}?)' || stop_re))[1];
  IF cand IS NULL THEN RETURN v_contact; END IF;     -- was: RETURN '{}'
  cand := btrim(regexp_replace(cand, '(?i)^(?:bestellt|bestimmt|ernannt|worden|zum|zur|ist|wird|hiermit|den|der|die|[:,]|\s)+', ''));

  v_name := btrim((regexp_match(cand, '^([^,]{3,90}?)\s*,'))[1]);
  IF v_name IS NOT NULL AND v_name ~ ('( |^)' || street_re) THEN v_name := NULL; END IF;
  IF v_name IS NOT NULL THEN
    tail := substr(cand, length(v_name) + 1);
  ELSE
    m := regexp_match(cand, '^(.+?) (' || street_re || '.*)$');
    IF m IS NOT NULL THEN v_name := btrim(m[1]); tail := m[2];
    ELSE
      m := regexp_match(cand, '^(.+) ((?:[A-ZÄÖÜ][A-Za-zäöüß.\-]+ ){0,2}?[A-ZÄÖÜ][A-Za-zäöüß.\-]+ \d+[a-z]?(?:[ ,].*)?)$');
      IF m IS NOT NULL THEN v_name := btrim(m[1]); tail := m[2];
      ELSE v_name := btrim((regexp_match(cand, '^([^,]{3,90})'))[1]); tail := ''; END IF;
    END IF;
  END IF;

  IF v_name IS NULL OR length(v_name) < 4 THEN RETURN v_contact; END IF;   -- was: RETURN '{}'
  v_name := btrim(regexp_replace(v_name, '\s+[A-ZÄÖÜ][A-Za-zäöüß.\-]*\.? \d+ ?[a-z]?$', ''));
  v_name := btrim(regexp_replace(v_name, '[ ,;:]+$', ''));

  IF lower(v_name) = ANY (ARRAY['entscheidung','stellung','insolvenzverwalter','insolvenzverwalters',
        'insolvenzverwalterin','sachwalter','sachwalterin','treuhänder','beschluss','verfügung',
        'vermögen','schuldner','schuldnerin','person','folgende','folgenden']) THEN
    RETURN v_contact;                                                       -- was: RETURN '{}'
  END IF;

  v_plz := btrim((regexp_match(tail, '(\d{5} [A-ZÄÖÜ][^,]{1,50}?)(?:,| Tel| Telefon| E-?Mail| Email| Internet| Fax| §|$)'))[1]);
  v_street := btrim((regexp_match(tail, '(?:^|,) *([^,]{2,60}?) *, *\d{5} '))[1]);
  IF v_plz IS NOT NULL AND v_street IS NOT NULL THEN v_addr := v_street || ', ' || v_plz; END IF;

  v_firm := btrim((regexp_match(tail, '^[ ,]*(?:c/o )?([^,]{2,80}?) *, *[^,]{2,60}?, *\d{5} '))[1]);
  v_firm := nullif(btrim(regexp_replace(coalesce(v_firm,''), '(?i)^(?:c/o |Gerichtsfach \d+.*|Postfach \d+.*)', '')), '');
  IF v_firm IS NOT NULL AND (v_firm !~ '[A-Za-zÄÖÜäöü]' OR v_firm ~ '^\d{5}' OR v_firm ~ ('^' || street_re)) THEN v_firm := NULL; END IF;

  has_title := v_name ~* title_re;
  IF v_name !~ '^[A-ZÄÖÜ]' THEN RETURN v_contact; END IF;                   -- was: RETURN '{}'
  IF NOT has_title AND v_addr IS NULL AND v_phone IS NULL AND v_email IS NULL THEN RETURN v_contact; END IF;

  RETURN jsonb_strip_nulls(jsonb_build_object('name', v_name, 'firm', v_firm, 'address', v_addr, 'phone', v_phone, 'email', v_email));
END $function$;

-- ---------------------------------------------------------------------------
-- 2. NULL-only backfill (APPROVAL-GATED — run AFTER section 1, in batches).
--    Re-fires the existing COALESCE-only trigger so only NULL admin columns are
--    filled; verified non-null values are NEVER overwritten. Cap each run.
-- ---------------------------------------------------------------------------
-- Dry-run estimate (read-only) — how many rows the improved parser would newly fill:
--   select count(*) from swift_v2.source_neu_insolvenz_announcements s
--   where nullif(btrim(s.announcement_text),'') is not null
--     and (s.insolvency_admin_email is null or s.insolvency_admin_phone is null
--          or s.insolvency_admin_address is null or s.insolvency_administrator is null)
--     and swift_v2.fn_parse_insolvency_admin(s.announcement_text) <> '{}'::jsonb;
--
-- Batched backfill (example cap 500/run; the trigger does the COALESCE fill):
--   with batch as (
--     select s.id from swift_v2.source_neu_insolvenz_announcements s
--     where nullif(btrim(s.announcement_text),'') is not null
--       and (s.insolvency_admin_email is null or s.insolvency_admin_phone is null
--            or s.insolvency_admin_address is null or s.insolvency_administrator is null)
--       and swift_v2.fn_parse_insolvency_admin(s.announcement_text) <> '{}'::jsonb
--     order by s.id limit 500)
--   update swift_v2.source_neu_insolvenz_announcements s
--     set updated_at = now() from batch b where s.id = b.id;
--   -- repeat until 0 rows; before/after: count non-null admin columns.

-- ---------------------------------------------------------------------------
-- 3. OPTIONAL — persist the structured subfields into public.apify_cases too.
--    Currently neu_insolvenz_fill_only_upsert writes only insolvency_administrator,
--    and apify_cases has insolvency_admin_name/address/phone/email but NO
--    insolvency_admin_firm column. The Cockpit does NOT read apify subfields
--    (it reads the swift columns from section 1), so this is hygiene/optional:
--      ALTER TABLE public.apify_cases ADD COLUMN IF NOT EXISTS insolvency_admin_firm text;
--      -- + extend neu_insolvenz_fill_only_upsert to COALESCE-write
--      --   insolvency_admin_name/firm/address/phone/email from the payload
--      --   (the scraper now sends them after the Phase 0044 code change).
-- =============================================================================
