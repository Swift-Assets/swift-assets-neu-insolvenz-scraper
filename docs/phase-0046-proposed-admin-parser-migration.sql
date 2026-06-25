-- =============================================================================
-- PHASE 0046 — admin-parser stop-set expansion (APPLIED in Phase 0046-B1)
-- =============================================================================
-- Status: APPLIED to project hqyktreytsjeirlpnnyr via migrations
-- phase_0046_b1_admin_parser_stop_expansion + phase_0046_b1_fix_postgres_greediness.
-- This file is the HISTORY/record of the live function (kept in sync with prod).
-- It carries NO raw data, performs NO overwrite, and the NULL-only backfill that
-- accompanies it (section 2) is approval-gated (Phase 0046-B2).
--
-- Mirrors the Phase 0046 Python fix in scraper.parse_insolvency_admin into the
-- DB parser swift_v2.fn_parse_insolvency_admin (which fills the Cockpit-facing
-- swift admin columns via the COALESCE-only trigger trg_fill_insolvency_admin).
--
-- POSTGRES-SPECIFIC FIX (Phase 0046-B1): applying the first draft of this
-- function still returned name-only / "{}" because PostgreSQL ties the WHOLE
-- regex's greediness to its FIRST quantifier (unlike Python, which evaluates
-- each quantifier independently). The greedy leading groups + connector
-- quantifier forced the (.{3,255}?) capture to run to end-of-text, overshooting
-- the PLZ. The leading optional groups and the connector run are therefore made
-- NON-GREEDY here: (?:zum |zur |zu )??, (?:vorl[äa]ufige[rn]? )??, {1,5}?.
-- This makes the whole RE non-greedy so the capture stops at the FIRST stop
-- token. The connector remnant ("bestellt: ", "ist:") is stripped by the
-- head-junk regex. (The Python parser does NOT need this — its engine differs.)
--
-- ROOT CAUSE (read-only diagnosis):
--   The trigger regex REQUIRES a trailing stop token after the captured name/
--   address. Real opening / securing-measure appointment blocks of the form
--     "Zum [vorläufigen] Insolvenzverwalter wird bestellt: Rechtsanwalt NAME,
--      Street, 12345 City. Verfügungen der Schuldnerin … (§ 21 …)"
--   end with a debtor-restriction clause ("Der/Die Schuldner(in)"), a
--   "Verfügung(en)" sentence, a "(§ …)" reference, or simply END OF TEXT —
--   none of which the old stop set matched. So the regex matched NOTHING and
--   the administrator block (name + address, sometimes phone/email) was lost.
--   Cases ending with "Die Gläubiger werden aufgefordert" worked, which is why
--   coverage was decent but not complete.
--
-- FIX (this migration): expand the stop set with "(§", the debtor-restriction
-- clauses, "Verfügung", and end-of-string ($); and strip a trailing sentence
-- period from the captured PLZ/city. Everything else is byte-identical to the
-- current (Phase 0044B) function. Name-anchoring guards are unchanged, so an
-- address is still never returned without a plausible administrator name and a
-- debtor name is never captured.
--
-- NOTE: the Python parser also iterates ALL trigger matches (so a generic
-- "Der Insolvenzverwalter ist berechtigt …" mention cannot shadow the real
-- appointment block). That edge case is ~3 rows in production and is NOT ported
-- to SQL here (the stop-set expansion is the 99% fix); it can be added later if
-- needed.
--
-- Dry-run estimate (read-only): ~28–36 existing name-NULL rows would gain a
-- NAME (mostly Restschuldbefreiung + a handful of Eröffnung/Sicherungsmaßnahmen);
-- the email/phone contact-only path is already covered by Phase 0044B. The
-- larger benefit is FUTURE inserts: every future opening/securing announcement
-- in the "wird bestellt: … (§/Verfügung/end)" form now extracts correctly.
--
-- Safety: CREATE OR REPLACE of an IMMUTABLE function; trigger stays COALESCE-only
-- (never overwrites non-null). No DROP/TRUNCATE/DELETE/ALTER, no RLS/grant/portal
-- change. Apply + NULL-only backfill in an approved Phase 0046B (see Section 2).
-- =============================================================================

-- 1. Function (only stop_re and the PLZ trailing-period strip changed vs 0044B)
CREATE OR REPLACE FUNCTION swift_v2.fn_parse_insolvency_admin(p_text text)
 RETURNS jsonb
 LANGUAGE plpgsql
 IMMUTABLE
AS $function$
DECLARE
  t text; cand text; tail text; m text[];
  v_name text; v_firm text; v_addr text; v_phone text; v_email text; v_street text; v_plz text;
  v_contact jsonb;
  has_title boolean;
  street_re constant text :=
    '(?:[A-ZÄÖÜ][A-Za-zäöüß.\-]*(?:[Ss]tra(?:ß|ss)e|[Ss]tr\.|[Aa]llee|[Ww]eg|[Pp]latz|[Rr]ing|[Gg]asse|[Dd]amm|[Uu]fer|[Ww]all|[Cc]haussee|[Ll]andstr\.?)'
    || '|[A-ZÄÖÜ][A-Za-zäöüß.\-]+ (?:Stra(?:ß|ss)e|Str\.|Allee|Weg|Platz|Ring|Gasse|Damm|Ufer|Wall|Chaussee))\.? \d+ ?[a-z]?';
  title_re constant text := '^(?:Rechtsanw[äa]lt(?:in)?|RA(?:in)?\.?|Dr\.|Prof\.|Dipl\.\-?\w*|Herr|Frau)';
  -- PHASE 0046: expanded stop set (added "(§", debtor clauses, Verfügung, end-of-string).
  stop_re constant text := '(?: Die (?:Insolvenz)?[Gg]läubiger| Insolvenzforderungen sind| Forderungen sind| §|\(§| \d\.? Die| Der Schuldner| Die Schuldner| Den Schuldner| Dem Schuldner| Verf[üu]gung| bestellt| ernannt| bestimmt|$)';
BEGIN
  IF p_text IS NULL OR btrim(p_text) = '' THEN RETURN '{}'::jsonb; END IF;
  t := regexp_replace(p_text, '\s+', ' ', 'g');
  v_email := (regexp_match(t, 'E-?Mail\s*:?\s*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', 'i'))[1];
  v_phone := btrim((regexp_match(t, 'Tel(?:efon)?\.?\s*:?\s*(\+?\d[\d /().\-]{4,}\d)', 'i'))[1]);

  IF (v_email IS NOT NULL OR v_phone IS NOT NULL)
     AND p_text ~* '(Insolvenzverwalter|Sachwalter|Treuh(ä|ae)nder)' THEN
    v_contact := jsonb_strip_nulls(jsonb_build_object('phone', v_phone, 'email', v_email));
  ELSE
    v_contact := '{}'::jsonb;
  END IF;

  -- Leading optional groups + connector run are NON-GREEDY (??, {1,5}?) so the
  -- whole RE is non-greedy and (.{3,255}?) stops at the FIRST stop token — see
  -- the POSTGRES-SPECIFIC FIX note in the header.
  cand := (regexp_match(t,
    '(?i)(?:zum |zur |zu )??(?:vorl[äa]ufige[rn]? )??(?:Insolvenzverwalter(?:in)?|Sachwalter(?:in)?|Treuh[äa]nder(?:in)?) '
    || '(?:(?:ist|wird|hiermit|bestellt|bestimmt|ernannt|worden|zum|zur)[\s:]+){1,5}?(.{3,255}?)' || stop_re))[1];
  IF cand IS NULL THEN RETURN v_contact; END IF;
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

  IF v_name IS NULL OR length(v_name) < 4 THEN RETURN v_contact; END IF;
  v_name := btrim(regexp_replace(v_name, '\s+[A-ZÄÖÜ][A-Za-zäöüß.\-]*\.? \d+ ?[a-z]?$', ''));
  v_name := btrim(regexp_replace(v_name, '[ ,;:]+$', ''));

  IF lower(v_name) = ANY (ARRAY['entscheidung','stellung','insolvenzverwalter','insolvenzverwalters',
        'insolvenzverwalterin','sachwalter','sachwalterin','treuhänder','beschluss','verfügung',
        'vermögen','schuldner','schuldnerin','person','folgende','folgenden']) THEN
    RETURN v_contact;
  END IF;

  v_plz := btrim((regexp_match(tail, '(\d{5} [A-ZÄÖÜ][^,]{1,50}?)(?:,| Tel| Telefon| E-?Mail| Email| Internet| Fax| §|$)'))[1]);
  v_plz := nullif(regexp_replace(coalesce(v_plz,''), '[.\s]+$', ''), '');   -- PHASE 0046: strip trailing period
  v_street := btrim((regexp_match(tail, '(?:^|,) *([^,]{2,60}?) *, *\d{5} '))[1]);
  IF v_plz IS NOT NULL AND v_street IS NOT NULL THEN v_addr := v_street || ', ' || v_plz; END IF;

  v_firm := btrim((regexp_match(tail, '^[ ,]*(?:c/o )?([^,]{2,80}?) *, *[^,]{2,60}?, *\d{5} '))[1]);
  v_firm := nullif(btrim(regexp_replace(coalesce(v_firm,''), '(?i)^(?:c/o |Gerichtsfach \d+.*|Postfach \d+.*)', '')), '');
  IF v_firm IS NOT NULL AND (v_firm !~ '[A-Za-zÄÖÜäöü]' OR v_firm ~ '^\d{5}' OR v_firm ~ ('^' || street_re)) THEN v_firm := NULL; END IF;

  has_title := v_name ~* title_re;
  IF v_name !~ '^[A-ZÄÖÜ]' THEN RETURN v_contact; END IF;
  IF NOT has_title AND v_addr IS NULL AND v_phone IS NULL AND v_email IS NULL THEN RETURN v_contact; END IF;

  RETURN jsonb_strip_nulls(jsonb_build_object('name', v_name, 'firm', v_firm, 'address', v_addr, 'phone', v_phone, 'email', v_email));
END $function$;

-- 2. NULL-only backfill (APPROVAL-GATED — same method as Phase 0044B):
--    keyset-paginated, 500/batch, set updated_at=now() to re-fire the
--    COALESCE-only trigger; never overwrites non-null admin values. Expected
--    ~28-36 new names (+ already-covered email/phone). Candidate set << 2,000.
--    See docs/phase-0044b-admin-backfill-results.md for the exact batch pattern.

-- 3. Synthetic verification (read-only) after apply — must return name+address:
--    select swift_v2.fn_parse_insolvency_admin(
--      'Zum vorläufigen Insolvenzverwalter wird bestellt: Rechtsanwalt Rüdiger '
--      'Wienberg, Düsseldorfer Straße 38, 10707 Berlin. Verfügungen der '
--      'Schuldnerin sind nur mit Zustimmung wirksam (§ 21 Abs. 2 Nr. 2 InsO).');
--    -- expect {"name":"Rechtsanwalt Rüdiger Wienberg","address":"Düsseldorfer Straße 38, 10707 Berlin"}
-- =============================================================================
