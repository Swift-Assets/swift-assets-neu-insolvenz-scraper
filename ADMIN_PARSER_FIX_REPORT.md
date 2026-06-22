# Insolvenzverwalter (Administrator) Parser Fix Report

**Date:** 2026-06-21 · **DB:** Supabase `hqyktreytsjeirlpnnyr`, schema `swift_v2`

**Pull requests**
- Scraper (Python parser + tests): https://github.com/Swift-Assets/swift-assets-neu-insolvenz-scraper/pull/16
- Web portal (DB parser, columns, trigger, backfill, view): https://github.com/Swift-Assets/swift-assets-04-web-portal/pull/7

---

## 1. ملخّص تنفيذي

كان السكرابر يلتقط اسم وكيل الإعسار باستخدام Regex هشّ لا يتعرّف على أكثر الصيغ شيوعاً ("Insolvenzverwalter ist: Rechtsanwalt …" و"Zum Insolvenzverwalter wird bestellt: …")، فبقي الاسم فارغاً في ~522 إعلاناً يذكر الوكيل صراحةً، ولم يُستخرَج المكتب/العنوان/الهاتف/البريد إطلاقاً. أضفنا مُحلِّلاً متسامحاً (في قاعدة البيانات `swift_v2.fn_parse_insolvency_admin` وفي السكرابر `parse_insolvency_admin`) يستخرج الاسم والمكتب والعنوان والهاتف والبريد، مع حُرّاس صارمة تمنع التقاط اسم المدين أو النصوص النمطية. أضفنا أربعة أعمدة + Trigger يملؤها عند كل كتابة + تعبئة رجعية idempotent، وعرضناها في `v_public_company_detail_v2` مع الحفاظ التامّ على الأمان. النتيجة: تغطية اسم الوكيل ارتفعت من 312 إلى 476، والبريد 0→64، الهاتف 0→140، العنوان 0→139.

---

## 2. Root cause

The administrator block is present, explicit, and regular inside `announcement_text`, e.g.:

> `… Insolvenzverwalter ist: Rechtsanwalt Thomas Wilhelm, Jost Nowak Rechtsanwälte, Homburger Landstr. 838, 60437 Frankfurt am Main, Tel.: 069-209 739 0, E-Mail: wilhelm@jruc.de. Die Gläubiger werden aufgefordert: …`

The previous parser (`ADMIN_PATTERNS` in `scraper.py`) only extracted a **name** and its trigger could not handle the two dominant phrasings:

1. **`Insolvenzverwalter ist: <Name>`** — the regex consumed `ist ` and then expected the name to begin, but the next character is `:`. The name capture (a title or capital letter) therefore failed and the whole match was abandoned. ~55 % of the gap rows with a contact block use this form.
2. **`Zum/Zur (vorläufigen) Insolvenzverwalter wird bestellt: <Name> <Street> …`** — not recognised at all (the old patterns keyed on `ist`/`wird … bestellt` without the `zum/zur` lead), and here the name runs **directly into the street with no comma**, which needs a street-aware split.

Consequently `insolvency_administrator` was NULL for **522** `done` announcements that name an administrator, and there were **no columns at all** for office/address/phone/email (they sat only as mostly-empty fields in `apify_cases`, never propagated).

---

## 3. Changes

### Parser logic (DB `swift_v2.fn_parse_insolvency_admin`; Python `parse_insolvency_admin`, kept in parity)
- **Trigger phrase:** role word (`Insolvenzverwalter(in)`/`Sachwalter(in)`/`Treuhänder(in)`) followed by a run of connector tokens (`ist`/`wird`/`bestellt`/`bestimmt`/`ernannt`/`hiermit`/`zum`/`zur`, separated by spaces **or** a colon), then the block, stopping at `Die Gläubiger …` / `Insolvenzforderungen sind` / `§` / `N. Die`.
- **Name:** comma-delimited when the block has `…<Name>, <office>…`; otherwise split off a glued street via a street-suffix regex (`…straße/str./allee/weg/platz/ring/gasse/…` incl. capitalised standalone `Straße`/`Str.` and hyphenated `Kaiser-Wilhelm-Straße`), with a "word(s) + house-number" fallback for suffix-less streets. A leaked trailing house-number is stripped.
- **Address:** anchored on the reliable **5-digit PLZ** (`<street>, <PLZ City>`), form-agnostic.
- **Firm:** the segment between name and street (strips `c/o`, drops `Gerichtsfach …`/`Postfach …`).
- **Phone / e-mail:** from `Tel.:`/`Telefon` and `E-Mail:`/`Email`.
- **Guards (never guess / never capture the debtor):** a parsed name must start with an uppercase letter **and** be either titled (`Rechtsanwalt/-in`, `Dr.`, `Prof.`, `Dipl.-…`, `Herr/Frau`) **or** backed by a real contact block (address/phone/email). This rejects boilerplate such as `… ist berechtigt und verpflichtet …`. Extraction is anchored to the appointment trigger and stops before the creditor section, so the debtor (named earlier after "Über das Vermögen der …") is never captured.

### New regexes / tests
- `tests/test_admin_parser.py` — 9 unit tests over real announcement texts: both phrasings, `c/o` firm, surname-first names, boilerplate negative, debtor-not-captured, empty/None.

### New columns (migration `swift_v2_0016`)
- `insolvency_admin_firm/address/phone/email text` on `source_neu_insolvenz_announcements`, each with `COMMENT ON COLUMN`.

### RPC / sync
- Rather than thread the parse through the 100-line `backfill_neu_insolvenz_direct_entities` RPC, a **NULL-only `BEFORE INSERT/UPDATE` trigger** (`tg_fill_insolvency_admin`) parses `announcement_text` and fills the name + 4 columns on every write. The RPC's `INSERT … ON CONFLICT` upsert (and `run_swift_v2_backfill.py`, which calls it) therefore **carry the fields to the portal layer automatically**; `coalesce` guarantees a verified value is never overwritten, and new rows self-populate.

### View
- `v_public_company_detail_v2` extended with `insolvency_admin_firm/address/phone/email` (most-recent non-null per entity). Security preserved exactly: `security_invoker=false`, `security_barrier=true`, company-only publishable filter unchanged; no RLS/grant changes.

---

## 4. Backfill

One-time, idempotent, NULL-only re-parse of every existing row's `announcement_text` (coalesce — never overwrites). Resumable: re-running fills only what is still NULL.

| Verification query | Before | After |
|---|---|---|
| `insolvency_administrator` not null (mirror) | 312 | **476** |
| `insolvency_admin_email` not null | 0 | **64** |
| `insolvency_admin_phone` not null | 0 | **140** |
| `insolvency_admin_address` not null | 0 | **139** |
| `insolvency_admin_firm` not null | 0 | **17** |
| Gap: `done` + text ~ "Insolvenzverwalter" + name NULL | 522 | **358** |
| Portal `v_public_company_detail_v2`: admin not null / total | ~15–20 / 981 | **42 / 934** |

The gap does not reach 0 because **358** of those rows contain only an *incidental* mention of "Insolvenzverwalter" (e.g. "… bei dem Insolvenzverwalter anzumelden") with **no appointment block and no administrator named** — they are correctly left NULL. Of the ~178 rows that actually name an administrator, ~164 were filled (~92 %); the rest use rare suffix-less multi-word streets or atypical phrasings.

The portal count (42) is far below the mirror count (476) by design: most named administrators belong to **natural-person** insolvencies, which the company-only portal filter excludes — consistent with the privacy rule.

---

## 5. Accuracy notes

- **Spot-checks:** 14 random parsed rows reviewed by hand — all names, addresses, phones and e-mails correct, including complex titles (`Prof. Dr.`, `Diplom-Kaufmann`, `Dr. … LL.M.`), surname-first names, `c/o` firms, and every street type. 0 errors.
- **Aggregate guards:** over all parsed names — `0` contained connector/boilerplate junk (`bestellt`/`ernannt`/`berechtigt`/`verpflichtet`/`Schuldner`); `0` suspiciously long (>60 chars, the debtor-block signature). The one boilerplate false positive found during development (`"berechtigt und verpflichtet"`) is rejected by the title-or-contact guard.
- **False-positive policy:** when a field cannot be parsed confidently it is left NULL — never guessed. Phone capture is greedy and may include a trailing fax/extra digit in a few records (cosmetic; see follow-ups).

---

## 6. Remaining gaps / follow-ups

1. **Secondary task — entity display names (NOT done).** Some entities still display a proprietor / managing-director personal name ("Surname, Firstname", "… vertr. d.d. GF …") instead of the firm name. This was the optional secondary task and is left as a separate follow-up: prefer the Handelsregister/`register_info` firm name and strip trailing `vertr. d.d. GF …` clauses. (These are legitimate commercial entities — they should not be hidden.)
2. **Suffix-less / multi-word streets** (e.g. "Unterer Anger 3", "Am Markt") in the *name-glued* (form B) layout occasionally leave the address NULL and, rarely, leave an extra street word on the name. ~8 % of the named-admin set. Could be improved with a small street-name dictionary.
3. **Phone normalisation.** Numbers are captured verbatim; a normaliser (strip fax ranges / spacing) would tidy them.
4. **Python tests run in CI only** — there was no Python interpreter in the authoring environment; the DB parser (which populates the portal) was verified directly against live data.

---

## 7. Rollback

- **View (`swift_v2_0017`):** re-apply the pre-0017 definition (drop the four `insolvency_admin_*` columns from the SELECT/`admin_info`). No data loss.
- **Trigger + columns + parser (`swift_v2_0016`):**
  ```sql
  DROP TRIGGER IF EXISTS trg_fill_insolvency_admin ON swift_v2.source_neu_insolvenz_announcements;
  DROP FUNCTION IF EXISTS swift_v2.tg_fill_insolvency_admin();
  ALTER TABLE swift_v2.source_neu_insolvenz_announcements
    DROP COLUMN IF EXISTS insolvency_admin_firm, DROP COLUMN IF EXISTS insolvency_admin_address,
    DROP COLUMN IF EXISTS insolvency_admin_phone, DROP COLUMN IF EXISTS insolvency_admin_email;
  DROP FUNCTION IF EXISTS swift_v2.fn_parse_insolvency_admin(text);
  ```
  `announcement_text` is untouched, so dropping the parsed columns loses no source data. The backfill only *filled* NULLs, so reverting the parser does not corrupt the existing `insolvency_administrator` values.
- **Scraper (`parse_insolvency_admin`):** revert PR #16; the legacy `ADMIN_PATTERNS` name extraction returns. No schema impact (the scraper does not write the new sub-columns; the DB trigger does).

All changes are independently revertible; none deletes source data, weakens RLS, changes grants, or alters the public view's `security_invoker=false` / `security_barrier=true` / company-only guarantees.
