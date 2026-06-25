"""
Neu-Insolvenzbekanntmachungen Scraper for Swift-Assets
=======================================================

Direct scraper for German insolvency announcements from
https://neu.insolvenzbekanntmachungen.de/

Two-phase design (cost-optimized for daily runs):
  Phase 1: Fetch listing rows (cheap: 1 search request → ~6000 rows)
  Phase 2: For each row NOT already in DB, fetch the full text via AJAX
           (1 AJAX click + 1 text.xhtml GET per new record)

Behavior:
  - UPSERTs into public.apify_cases (uses existing unique constraint)
  - Skips detail-fetch for records already present (deduped by unique key)
  - Optional MAX_DETAIL_FETCHES cap to bound runtime/cost
  - Concurrent detail fetching (configurable, default 4 workers)

Environment variables:
    SUPABASE_URL                — required (unless DRY_RUN=1)
    SUPABASE_SERVICE_ROLE_KEY   — required (unless DRY_RUN=1)
    SCRAPE_DATE_FROM            — YYYY-MM-DD (default: today)
    SCRAPE_DATE_TO              — YYYY-MM-DD (default: today)
    MAX_DETAIL_FETCHES          — int (default: 5000). Half is reserved for new
                                  rows so backfill cannot starve them.
    MAX_DETAIL_ATTEMPTS         — int (default: 5). An empty-text row drops out
                                  of the backfill set after this many fetch
                                  attempts (tracked in detail_fetch_attempts).
    DETAIL_WORKERS              — int (default: 6)
    DRY_RUN                     — "1" to skip DB writes
    SKIP_DETAILS                — "1" to only upsert listing-level data
    WRITE_PHASE_FIELDS          — default ON: write insolvency_phase /
                                  is_pre_verteilung columns (they exist on
                                  apify_cases). Set to "0" to disable; fields
                                  are still computed and shown in DRY_RUN.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from threading import Lock
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://neu.insolvenzbekanntmachungen.de"
SEARCH_PATH = "/ap/suche.jsf"
RESULT_PATH = "/ap/ergebnis.jsf"
TEXT_PATH = "/ap/text.xhtml"
SOURCE_ACTOR = "neu_insolvenz_direct"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

REQUEST_TIMEOUT_SEC = 30
MAX_RETRIES = 3
DEFAULT_DETAIL_WORKERS = 6
DEFAULT_MAX_DETAIL_FETCHES = 5000
DEFAULT_MAX_DETAIL_ATTEMPTS = 5   # give up re-fetching an empty row after N tries
# Phase 4 (2026-06-20): bumped from 4/1500/3 to 6/5000/5. The daily portal
# volume is ~3k-5k new announcements; the old budget could only fetch ~1/3 of
# them, leaving ~3k empty-text rows per day that the swift_v2 enrichment queue
# could not classify by phase. New budget covers a full day plus drains backlog.
DETAIL_FETCH_DELAY_SEC = 0.2   # per worker

# Write the computed phase columns (insolvency_phase, is_pre_verteilung) to the
# DB. The columns exist on public.apify_cases and the fill-only RPC accepts
# them, so the safe default is ON. Set WRITE_PHASE_FIELDS=0 to disable (the
# fields are still computed and shown in DRY_RUN output).
WRITE_PHASE_FIELDS = os.getenv("WRITE_PHASE_FIELDS", "1") != "0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("neu_insolvenz")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

def _norm_key_val(v: Any) -> Any:
    """Normalize a unique-key field so the scraper side and the DB side compare
    equal. A DB value stored as ``''`` or with stray surrounding whitespace must
    match a scraper-side ``None`` / trimmed value. Non-strings (already-ISO
    dates are plain strings, so this only ever sees strings or None) pass through
    unchanged."""
    if isinstance(v, str):
        return v.strip() or None
    return v


@dataclass
class InsolvencyRecord:
    """A single insolvency announcement, mapped to public.apify_cases columns."""
    source_actor: str = SOURCE_ACTOR
    source_run_id: str | None = None
    announcement_date: str | None = None       # YYYY-MM-DD
    case_number: str | None = None
    court: str | None = None
    debtor_name: str | None = None
    debtor_city: str | None = None
    register_info: str | None = None
    registry_court: str | None = None
    registry_type: str | None = None
    registry_number: str | None = None
    announcement_text: str | None = None
    insolvency_administrator: str | None = None
    opening_date: str | None = None
    claims_deadline: str | None = None
    announcement_type_hint: str | None = None
    insolvency_phase: str | None = None        # see classify_phase()
    is_pre_verteilung: bool | None = None       # True = potentially acquirable
    # ---- Source-identity / provenance fields (Phase 0037D) -----------------
    # source_url     : page-level provenance (the public portal page the row is
    #                  published on). The JSF source exposes NO per-announcement
    #                  GET permalink, so this is page-level, not a unique deep
    #                  link — never fabricated into a fake per-row URL.
    # content_hash   : deterministic stable identity over the proceeding key +
    #                  full text. Always computable, even when announcement_text
    #                  is empty, so a row has a stable external id from day one.
    # announcement_type_raw : the umlaut-folded keyword that produced
    #                  announcement_type_hint, preserved so the canonicalization
    #                  is auditable/reversible (kept in raw_json, never guessed).
    # insolvenzindex_*      : populated only if the source ever exposes them
    #                  (it does not via the current JSF flow) — never fabricated.
    source_url: str | None = None
    content_hash: str | None = None
    announcement_type_raw: str | None = None
    insolvenzindex_publication_id: str | None = None
    insolvenzindex_company_id: str | None = None
    scraped_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # transient — not stored in DB, used internally
    _row_index: int = field(default=-1, repr=False)
    _button_id: str | None = field(default=None, repr=False)
    # DB primary key (apify_cases.id is a UUID/text), only set for existing rows
    # we backfill (so we can bump detail_fetch_attempts by id afterwards). None
    # for brand-new listing rows.
    _db_id: str | None = field(default=None, repr=False)

    def unique_key(self) -> tuple:
        """Stable identity used for dedup and re-fetch matching.

        Intentionally EXCLUDES ``announcement_type_hint``: that field is NULL
        at listing time and only gets a value after detail-fetch/classification,
        so including it made the same announcement hash differently before vs
        after enrichment, breaking dedup and re-fetch matching. The fields below
        are all known at listing time and mirror the DB RPC's stable-match logic
        (``neu_insolvenz_fill_only_upsert`` matches on exactly these and treats
        ``announcement_type_hint`` as a fill-only/guarded column).

        Each field is passed through ``_norm_key_val`` so a DB value stored as
        ``''`` or with stray whitespace matches the scraper-side ``None``/trimmed
        value — see ``SupabaseClient._stable_key_from_row`` which applies the
        same normalization."""
        return tuple(
            _norm_key_val(v) for v in (
                self.court, self.case_number, self.announcement_date,
                self.registry_court, self.registry_type, self.registry_number,
            )
        )

    def to_db_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop internal fields
        d.pop("_row_index", None)
        d.pop("_button_id", None)
        d.pop("_db_id", None)
        # The phase fields are always computed (and used in dry-run output) and
        # sent to the DB by default (the columns exist on public.apify_cases and
        # the fill-only RPC accepts them). is_pre_verteilung is kept as a native
        # bool here so the JSON payload carries a true boolean (the RPC checks
        # jsonb_typeof = 'boolean'). Set WRITE_PHASE_FIELDS=0 to omit them.
        if not WRITE_PHASE_FIELDS:
            d.pop("insolvency_phase", None)
            d.pop("is_pre_verteilung", None)
        # Never send the "unknown" placeholder: an unknown/ambiguous phase is
        # the same as "not classified", so emit it as None (dropped below) so it
        # can never overwrite or block a later real classification. (The RPC also
        # guards this, but we keep the scraper honest too.)
        elif d.get("insolvency_phase") == PHASE_UNKNOWN:
            d["insolvency_phase"] = None
        # ---- Source-identity / provenance (Phase 0037D) --------------------
        # Always attach a deterministic content_hash (stable external id) and a
        # raw_json/external_raw provenance object. These are forward-compatible
        # groundwork: the CURRENT public.neu_insolvenz_fill_only_upsert RPC reads
        # only a fixed column list and silently ignores the extra keys, and
        # public.apify_cases has no source_url/content_hash columns yet — so they
        # are persisted ONLY once the proposed migration in
        # docs/phase-0037d-proposed-migration.sql extends the RPC + the
        # swift_v2 sync to consume them (Phase 0037E/0037F). Sending them now is
        # harmless (ignored keys) and lets the migration land without a second
        # scraper change.
        if not d.get("content_hash"):
            d["content_hash"] = self.compute_content_hash()
        d["external_raw"] = self.build_external_raw(d["content_hash"])
        # Keep nullable fields part of the unique constraint even if None
        keep_null = {
            "announcement_date", "case_number", "court",
            "announcement_type_hint", "registry_court",
            "registry_type", "registry_number",
        }
        return {k: v for k, v in d.items() if v is not None or k in keep_null}

    def compute_content_hash(self) -> str:
        """Deterministic stable identity for the announcement.

        Hashes the stable proceeding key (court/case/date/registry/type) plus
        the announcement text, so the SAME announcement always yields the SAME
        hash and a row has a usable external id even before its detail text is
        fetched. SHA-256 over a pipe-joined, normalized basis. (Distinct from
        the downstream ``md5(announcement_text)`` the swift_v2 sync computes:
        that hashes only the body; this anchors the proceeding identity.)"""
        basis = "|".join(
            (_norm_key_val(v) or "") for v in (
                self.court, self.case_number, self.announcement_date,
                self.registry_court, self.registry_type, self.registry_number,
                self.announcement_type_hint, self.announcement_text,
            )
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def build_external_raw(self, content_hash: str) -> dict[str, Any]:
        """Provenance object mirrored into raw_json downstream. Holds the raw
        (folded) announcement-type keyword, the content hash, the page-level
        source URL and any source ids — never debtor identity, so it is safe to
        store for natural-person rows."""
        return {
            "source_actor": self.source_actor,
            "source_url": self.source_url,
            "content_hash": content_hash,
            "announcement_type_hint": self.announcement_type_hint,
            "announcement_type_raw": self.announcement_type_raw,
            "insolvenzindex_publication_id": self.insolvenzindex_publication_id,
            "insolvenzindex_company_id": self.insolvenzindex_company_id,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GERMAN_MONTHS = {
    "Januar": 1, "Februar": 2, "März": 3, "Maerz": 3, "April": 4,
    "Mai": 5, "Juni": 6, "Juli": 7, "August": 8, "September": 9,
    "Oktober": 10, "November": 11, "Dezember": 12,
}


def parse_german_date(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    m = re.match(r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\s+(\d{4})", s)
    if m:
        d_str, mo_str, y_str = m.groups()
        mo = GERMAN_MONTHS.get(mo_str)
        if mo:
            try:
                return date(int(y_str), mo, int(d_str)).isoformat()
            except ValueError:
                return None
    return None


def parse_register_info(register_info: str | None) -> dict[str, str | None]:
    out = {"registry_court": None, "registry_type": None, "registry_number": None}
    if not register_info or not register_info.strip():
        return out
    s = register_info.strip()
    m = re.match(r"([^,]+),\s*(HRB|HRA|VR|PR|GnR|GsR)\s+(.+)", s)
    if m:
        out["registry_court"] = m.group(1).strip()
        out["registry_type"] = m.group(2).strip()
        out["registry_number"] = m.group(3).strip()
        return out
    m = re.match(r"(.+?)\s+(HRB|HRA|VR|PR|GnR|GsR)\s+(\S+)", s)
    if m:
        out["registry_court"] = m.group(1).strip()
        out["registry_type"] = m.group(2).strip()
        out["registry_number"] = m.group(3).strip()
    return out


# ----- Announcement type detection ------------------------------------------

# Ordered, umlaut-FOLDED keyword -> canonical announcement-type label.
#
# Matching is done against the umlaut-/ß-folded, whitespace-normalized text
# (see _normalize_de), so every keyword here is written in its folded form
# ("ä"->"ae", "ö"->"oe", "ü"->"ue", "ß"->"ss"). This is the Phase 0037D
# hardening: the previous list matched raw `.lower()` text, so portal spellings
# using ae/oe/ue/ss slipped through as NULL. Order matters — most specific
# first so "Schlussverteilung" beats "Verteilung", "... mangels Masse" beats a
# bare verb, and a preliminary-administration phrase beats a bare "Anordnung".
TYPE_KEYWORDS: list[tuple[str, str]] = [
    # Closure / dismissal (most specific multiword first)
    ("abweisung mangels masse", "Abweisung mangels Masse"),
    ("einstellung mangels masse", "Einstellung mangels Masse"),
    ("masseunzulaenglichkeit", "Masseunzulänglichkeit"),
    ("restschuldbefreiung", "Restschuldbefreiung"),
    # Remuneration
    ("verguetungsfestsetzung", "Vergütungsfestsetzung"),
    ("verguetung", "Vergütungsfestsetzung"),
    # Securing measures / preliminary administration
    ("anordnung der vorlaeufigen", "Sicherungsmaßnahmen"),
    ("vorlaeufige insolvenzverwalt", "Sicherungsmaßnahmen"),
    ("vorlaeufiger insolvenzverwalt", "Sicherungsmaßnahmen"),
    ("sicherungsmassnahme", "Sicherungsmaßnahmen"),
    # Final distribution / closing date (before generic Verteilung)
    ("schlussverteilung", "Schlussverteilung"),
    ("schlusstermin", "Schlussverteilung"),
    # Realisation / distribution
    ("masseverwertung", "Verwertung"),
    ("verwertung", "Verwertung"),
    ("verteilungsverzeichnis", "Verteilung"),
    ("verteilung", "Verteilung"),
    # Hearings
    ("berichtstermin", "Berichtstermin"),
    ("pruefungstermin", "Prüfungstermin"),
    # Lifting of proceedings
    ("aufhebung", "Aufhebung"),
    ("aufgehoben", "Aufhebung"),
    # Opening (specific phrasings before bare stems)
    ("eroeffnung des insolvenzverfahrens", "Eröffnung"),
    ("insolvenzverfahren eroeffnet", "Eröffnung"),
    ("eroeffnung", "Eröffnung"),
    ("eroeffnet", "Eröffnung"),
    # Discontinuation (generic, after "mangels Masse" above)
    ("einstellung", "Einstellung"),
    ("eingestellt", "Einstellung"),
    # Orders (generic, after the preliminary-administration phrases above)
    ("anordnung", "Anordnung"),
    ("angeordnet", "Anordnung"),
    # Appointment (lowest priority — most openings also mention it)
    ("bestellung", "Bestellung"),
    ("bestellt", "Bestellung"),
]


def detect_announcement_type_full(text: str) -> tuple[str | None, str | None]:
    """Detect the announcement type from the FULL announcement text.

    Returns ``(canonical_hint, raw_keyword)``:
      - ``canonical_hint``: a clean canonical label (e.g. ``"Eröffnung"``), or
        None when the text contains no recognizable type. NEVER fabricated and
        NEVER inferred from the debtor name or natural-person status — only from
        the announcement body itself.
      - ``raw_keyword``: the umlaut-folded source keyword that matched, kept in
        raw_json so the canonicalization stays auditable/reversible.

    Matching uses ``_normalize_de`` so ä/ae, ö/oe, ü/ue and ß/ss spellings all
    resolve to the same canonical label."""
    if not text:
        return None, None
    norm = _normalize_de(text)
    for keyword, label in TYPE_KEYWORDS:
        if keyword in norm:
            return label, keyword
    return None, None


def detect_announcement_type(text: str) -> str | None:
    """Backward-compatible wrapper returning only the canonical hint."""
    return detect_announcement_type_full(text)[0]


# ----- Insolvency phase classification --------------------------------------
#
# Determines the lifecycle phase of a case from the FULL announcement text and
# whether the company is still PRE-VERTEILUNG (pre-distribution = potentially
# acquirable). Returns phase enum values the downstream swift_v2 layer already
# consumes; when genuinely ambiguous we keep "unknown" rather than guessing.

PHASE_UNKNOWN = "unknown"


def _normalize_de(text: str) -> str:
    """Lower-case and fold umlaut/ß + whitespace variants so keyword matching
    is tolerant of how the portal spells things (ä/ae, ö/oe, ü/ue, ß/ss)."""
    t = text.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t)


# Late-stage / "dead" signals — case is winding down or closed, NOT acquirable.
# (label is matched against the umlaut-folded, whitespace-normalized text)
LATE_STAGE_SIGNALS: list[tuple[str, str]] = [
    ("schlusstermin", "schlusstermin"),
    ("schlussverteilung", "schlussverteilung"),
    ("verteilungsverzeichnis", "verteilungsverzeichnis"),
    ("aufhebung des insolvenzverfahrens", "aufhebung"),
    ("einstellung mangels masse", "dismissed_lack_of_assets"),
    ("abweisung mangels masse", "dismissed_lack_of_assets"),
    ("masseunzulaenglichkeit", "masseunzulaenglichkeit"),
    ("restschuldbefreiung", "restschuldbefreiung"),
]

# Pre-distribution / still-open signals — case is opening or under
# administration, company is potentially acquirable. Ordered most-specific
# first so e.g. preliminary administration wins over a bare "eroeffnung".
PRE_VERTEILUNG_SIGNALS: list[tuple[str, str]] = [
    ("vorlaeufige insolvenzverwaltung", "preliminary_administration"),
    ("vorlaeufiger insolvenzverwalter", "preliminary_administration"),
    ("vorlaeufige insolvenzverwalterin", "preliminary_administration"),
    ("anordnung der vorlaeufigen", "preliminary_administration"),
    ("sicherungsmassnahmen", "preliminary_administration"),
    ("sicherungsmassnahme", "preliminary_administration"),
    ("eroeffnung des insolvenzverfahrens", "opening"),
    ("insolvenzverfahren eroeffnet", "opening"),
    ("insolvenzverwalter bestellt", "administrator_appointed"),
    ("insolvenzverwalterin bestellt", "administrator_appointed"),
    ("bestellung des insolvenzverwalters", "administrator_appointed"),
    ("bestellung der insolvenzverwalterin", "administrator_appointed"),
    ("zum insolvenzverwalter", "administrator_appointed"),
    ("zur insolvenzverwalterin", "administrator_appointed"),
]


def classify_phase(text: str | None) -> tuple[str, bool | None]:
    """Classify the insolvency phase from the full announcement text.

    Returns ``(phase, is_pre_verteilung)``:
      - ``phase``            : one of the enum values above, or ``"unknown"``.
      - ``is_pre_verteilung``: True when an opening/preliminary/administrator
        signal is present AND no late-stage signal is present; False when any
        late-stage signal is present; None when the phase is unknown.

    Late-stage signals take precedence: an announcement can mention both the
    original opening and a closing event, but the presence of any closing
    signal means the company is no longer acquirable."""
    if not text or not text.strip():
        return PHASE_UNKNOWN, None

    norm = _normalize_de(text)

    late_phase: str | None = None
    for needle, label in LATE_STAGE_SIGNALS:
        if needle in norm:
            late_phase = label
            break

    if late_phase is not None:
        # Any late-stage signal => not acquirable, regardless of opening text.
        return late_phase, False

    for needle, label in PRE_VERTEILUNG_SIGNALS:
        if needle in norm:
            return label, True

    return PHASE_UNKNOWN, None


# ----- Field extraction from full text --------------------------------------

OPENING_PATTERNS = [
    re.compile(r"am\s+(\d{1,2}\.\d{1,2}\.\d{4})[^.]{0,80}?er[öo]ffnet", re.I),
    re.compile(r"er[öo]ffnet\s+am\s+(\d{1,2}\.\d{1,2}\.\d{4})", re.I),
    re.compile(r"Er[öo]ffnung\s*(?:am|:|datum)?\s*(\d{1,2}\.\d{1,2}\.\d{4})", re.I),
]

CLAIMS_PATTERNS = [
    re.compile(r"Forderungen[^.]{0,150}?bis\s+(?:zum\s+)?(\d{1,2}\.\d{1,2}\.\d{4})", re.I | re.S),
    re.compile(r"Anmeldefrist[^.]{0,80}?(\d{1,2}\.\d{1,2}\.\d{4})", re.I | re.S),
    re.compile(r"anzumelden[^.]{0,80}?bis\s+(?:zum\s+)?(\d{1,2}\.\d{1,2}\.\d{4})", re.I | re.S),
]

ADMIN_PATTERNS = [
    # "des Insolvenzverwalters Rechtsanwalt Peter Staroselski"
    # "Insolvenzverwalter wird Rechtsanwalt Hans Mueller bestellt"
    # "zur Insolvenzverwalterin wird Frau Anna Schmidt bestellt"
    re.compile(
        r"(?:vorl[äa]ufige[rnms]*\s+)?Insolvenzverwalter(?:in|s|n)?\s*(?:wird\s+|ist\s+|:|,)?\s*"
        r"((?:Frau\s+|Herr\s+|Rechtsanw[äa]lt(?:in)?\s+|RA\s+|RAin\s+|Dr\.\s+|Prof\.\s+|Prof\.\s+Dr\.\s+)*[A-ZÄÖÜ][\w\.\-]+(?:\s+[A-ZÄÖÜ][\w\.\-]+){0,4})"
    ),
    re.compile(
        r"(?:zum?\s+|zur\s+)?Insolvenzverwalter(?:in)?\s+(?:wird|ist|bestellt|ernannt)\s+"
        r"((?:Frau\s+|Herr\s+|Rechtsanw[äa]lt(?:in)?\s+|RA\s+|RAin\s+|Dr\.\s+|Prof\.\s+)*[A-ZÄÖÜ][\w\.\-]+(?:\s+[A-ZÄÖÜ][\w\.\-]+){0,4})"
    ),
    re.compile(
        r"Treuh[äa]nder(?:in)?\s*(?:wird|ist|bestellt|ernannt|:|,)?\s*"
        r"((?:Frau\s+|Herr\s+|Rechtsanw[äa]lt(?:in)?\s+|RA\s+|RAin\s+|Dr\.\s+|Prof\.\s+)*[A-ZÄÖÜ][\w\.\-]+(?:\s+[A-ZÄÖÜ][\w\.\-]+){0,4})"
    ),
]


# ---------------------------------------------------------------------------
# Insolvenzverwalter (administrator) extraction
# ---------------------------------------------------------------------------
# Kept in PARITY with the DB function swift_v2.fn_parse_insolvency_admin so the
# apify-side name and the portal-side structured columns agree. Handles the two
# dominant German phrasings (and Sachwalter/Treuhänder):
#   A) "... Insolvenzverwalter ist: Rechtsanwalt <Name>, <Firm?>, <Street>,
#       <PLZ City>, Tel.: <phone>, E-Mail: <email>."   (comma-delimited)
#   B) "Zum Insolvenzverwalter wird bestellt: <Name> <Street>, <PLZ City>
#       Telefon: <phone> Email: <email>"               (name glued to street)
# Anchors the office address on the 5-digit PLZ and only parses the
# administrator block (stops at "Die Gläubiger ..." / "§"). Unparseable fields
# are left None — never guessed. Guards against capturing the debtor or
# boilerplate: a parsed name must be titled OR backed by a real contact block.
_ADMIN_STREET_RE = (
    r"(?:[A-ZÄÖÜ][A-Za-zäöüß.\-]*(?:[Ss]tra(?:ß|ss)e|[Ss]tr\.|[Aa]llee|[Ww]eg|[Pp]latz|[Rr]ing|[Gg]asse|[Dd]amm|[Uu]fer|[Ww]all|[Cc]haussee|[Ll]andstr\.?)"
    r"|[A-ZÄÖÜ][A-Za-zäöüß.\-]+ (?:Stra(?:ß|ss)e|Str\.|Allee|Weg|Platz|Ring|Gasse|Damm|Ufer|Wall|Chaussee))\.? \d+ ?[a-z]?"
)
_ADMIN_STOP_RE = r"(?: Die (?:Insolvenz)?[Gg]läubiger| Insolvenzforderungen sind| Forderungen sind| §| \d\.? Die)"
_ADMIN_TRIGGER = re.compile(
    r"(?:zum |zur |zu )?(?:vorl[äa]ufige[rn]? )?"
    r"(?:Insolvenzverwalter(?:in)?|Sachwalter(?:in)?|Treuh[äa]nder(?:in)?) "
    r"(?:(?:ist|wird|hiermit|bestellt|bestimmt|ernannt|worden|zum|zur)[\s:]+){1,5}"
    r"(.{3,255}?)" + _ADMIN_STOP_RE,
    re.I,
)
_ADMIN_TITLE = re.compile(r"^(?:Rechtsanw[äa]lt(?:in)?|RA(?:in)?\.?|Dr\.|Prof\.|Dipl\.\-?\w*|Herr|Frau)", re.I)
_ADMIN_EMAIL = re.compile(r"E-?Mail\s*:?\s*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", re.I)
_ADMIN_PHONE = re.compile(r"Tel(?:efon)?\.?\s*:?\s*(\+?\d[\d /().\-]{4,}\d)", re.I)
_ADMIN_HEAD_JUNK = re.compile(r"^(?:bestellt|bestimmt|ernannt|worden|zum|zur|ist|wird|hiermit|den|der|die|[:,]|\s)+", re.I)
_ADMIN_NAME_COMMA = re.compile(r"^([^,]{3,90}?)\s*,")
_ADMIN_NAME_FALLBACK = re.compile(r"^([^,]{3,90})")
_ADMIN_STREET_SPLIT = re.compile(r"^(.+?) (" + _ADMIN_STREET_RE + r".*)$")
_ADMIN_HOUSENUM_SPLIT = re.compile(
    r"^(.+) ((?:[A-ZÄÖÜ][A-Za-zäöüß.\-]+ ){0,2}?[A-ZÄÖÜ][A-Za-zäöüß.\-]+ \d+[a-z]?(?:[ ,].*)?)$"
)
_ADMIN_CONTAINS_STREET = re.compile(r"( |^)" + _ADMIN_STREET_RE)
_ADMIN_NAME_TAIL_NUM = re.compile(r"\s+[A-ZÄÖÜ][A-Za-zäöüß.\-]*\.? \d+ ?[a-z]?$")
_ADMIN_PLZ = re.compile(r"(\d{5} [A-ZÄÖÜ][^,]{1,50}?)(?:,| Tel| Telefon| E-?Mail| Email| Internet| Fax| §|$)")
_ADMIN_STREET_SEG = re.compile(r"(?:^|,) *([^,]{2,60}?) *, *\d{5} ")
_ADMIN_FIRM = re.compile(r"^[ ,]*(?:c/o )?([^,]{2,80}?) *, *[^,]{2,60}?, *\d{5} ")


def parse_insolvency_admin(text: str | None) -> dict[str, str | None]:
    """Extract the Insolvenzverwalter block from ``announcement_text``.

    Returns ``{name, firm, address, phone, email}`` — any field may be None.
    Mirrors swift_v2.fn_parse_insolvency_admin (the DB backfill/trigger parser).
    """
    out: dict[str, str | None] = {
        "name": None, "firm": None, "address": None, "phone": None, "email": None,
    }
    if not text or not text.strip():
        return out
    t = re.sub(r"\s+", " ", text)

    me = _ADMIN_EMAIL.search(t)
    email = me.group(1) if me else None
    mp = _ADMIN_PHONE.search(t)
    phone = mp.group(1).strip() if mp else None

    mt = _ADMIN_TRIGGER.search(t)
    if not mt:
        return out
    cand = _ADMIN_HEAD_JUNK.sub("", mt.group(1)).strip()

    name: str | None = None
    tail: str | None = None
    mc = _ADMIN_NAME_COMMA.match(cand)
    if mc:
        name = mc.group(1).strip()
        if _ADMIN_CONTAINS_STREET.search(name):
            name = None  # name ran into the street → not comma-delimited
    if name is not None:
        tail = cand[len(name):]
    else:
        ms = _ADMIN_STREET_SPLIT.match(cand)
        if ms:
            name, tail = ms.group(1).strip(), ms.group(2)
        else:
            mh = _ADMIN_HOUSENUM_SPLIT.match(cand)
            if mh:
                name, tail = mh.group(1).strip(), mh.group(2)
            else:
                mf = _ADMIN_NAME_FALLBACK.match(cand)
                name, tail = (mf.group(1).strip() if mf else None), ""

    if not name or len(name) < 4:
        return out
    name = _ADMIN_NAME_TAIL_NUM.sub("", name).strip()
    name = re.sub(r"[ ,;:]+$", "", name).strip()

    tail = tail or ""
    plz_m = _ADMIN_PLZ.search(tail)
    plz = plz_m.group(1).strip() if plz_m else None
    st_m = _ADMIN_STREET_SEG.search(tail)
    street = st_m.group(1).strip() if st_m else None
    address = f"{street}, {plz}" if (plz and street) else None

    firm: str | None = None
    fm = _ADMIN_FIRM.match(tail)
    if fm:
        firm = re.sub(r"(?i)^(?:c/o |Gerichtsfach \d+.*|Postfach \d+.*)", "", fm.group(1)).strip()
        if (not firm or not re.search(r"[A-Za-zÄÖÜäöü]", firm)
                or re.match(r"\d{5}", firm) or re.match(_ADMIN_STREET_RE, firm)):
            firm = None

    has_title = bool(_ADMIN_TITLE.match(name))
    if not re.match(r"[A-ZÄÖÜ]", name):
        return out
    if not has_title and not address and not phone and not email:
        return out  # reject debtor/boilerplate captures

    out.update(name=name, firm=firm, address=address, phone=phone, email=email)
    return out


def extract_fields_from_text(text: str) -> dict[str, Any]:
    phase, is_pre = classify_phase(text)
    type_hint, type_raw = detect_announcement_type_full(text)
    out: dict[str, Any] = {
        "insolvency_administrator": None,
        "insolvency_admin_firm": None,
        "insolvency_admin_address": None,
        "insolvency_admin_phone": None,
        "insolvency_admin_email": None,
        "opening_date": None,
        "claims_deadline": None,
        "announcement_type_hint": type_hint,
        "announcement_type_raw": type_raw,
        "insolvency_phase": phase,
        "is_pre_verteilung": is_pre,
    }
    if not text:
        return out

    admin = parse_insolvency_admin(text)
    out["insolvency_administrator"] = admin["name"]
    out["insolvency_admin_firm"] = admin["firm"]
    out["insolvency_admin_address"] = admin["address"]
    out["insolvency_admin_phone"] = admin["phone"]
    out["insolvency_admin_email"] = admin["email"]

    for pat in OPENING_PATTERNS:
        m = pat.search(text)
        if m:
            parsed = parse_german_date(m.group(1))
            if parsed:
                out["opening_date"] = parsed
                break

    for pat in CLAIMS_PATTERNS:
        m = pat.search(text)
        if m:
            parsed = parse_german_date(m.group(1))
            if parsed:
                out["claims_deadline"] = parsed
                break

    # Legacy fallback: only if the structured parser above found no name (e.g.
    # genitive "des Insolvenzverwalters Rechtsanwalt X" with no ist/wird/bestellt
    # connector). Never overwrites a structured-parser result.
    if out["insolvency_administrator"] is None:
        for pat in ADMIN_PATTERNS:
            m = pat.search(text)
            if m:
                name = m.group(1).strip().rstrip(",;.:")
                if 3 < len(name) < 120:
                    out["insolvency_administrator"] = name
                    break

    return out


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class NeuInsolvenzScraper:
    """Manages a JSF session. Note: detail fetching uses the SAME session
    (cannot be parallelized across sessions because text.xhtml depends on
    the last AJAX click in that JSESSIONID). For concurrency we open
    multiple parallel sessions."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
        })
        self.view_state: str | None = None
        self.jsessionid: str | None = None
        self._lock = Lock()  # serializes detail fetches within one session

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.request(
                    method, url, timeout=REQUEST_TIMEOUT_SEC, **kwargs
                )
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                wait = 2 ** attempt
                log.warning("Request failed (%d/%d): %s — retrying in %ds",
                            attempt, MAX_RETRIES, exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"Request failed after {MAX_RETRIES} attempts: {last_exc}")

    def initialize_session(self) -> None:
        resp = self._request("GET", f"{BASE_URL}{SEARCH_PATH}")
        soup = BeautifulSoup(resp.text, "html.parser")
        vs = soup.find("input", {"name": "jakarta.faces.ViewState"})
        if not vs or not vs.get("value"):
            raise RuntimeError("Could not extract jakarta.faces.ViewState from search page")
        self.view_state = vs["value"]
        self.jsessionid = self.session.cookies.get("JSESSIONID")
        if not self.jsessionid:
            raise RuntimeError("No JSESSIONID cookie received")

    def search(self, date_from: date, date_to: date) -> list[InsolvencyRecord]:
        if not self.view_state or not self.jsessionid:
            raise RuntimeError("Session not initialized")

        payload = {
            "frm_suche": "frm_suche",
            "frm_suche:lsom_bundesland:codelist:scl_bundesland:mysom": "NO_CODE",
            "frm_suche:lsi_insolvenzgerichte:codelist:scl_insolvenzgericht:mysom": "NO_CODE",
            "frm_suche:ldi_datumVon:datumHtml5": date_from.isoformat(),
            "frm_suche:ldi_datumBis:datumHtml5": date_to.isoformat(),
            "frm_suche:lsom_wildcard:lsom": "0",
            "frm_suche:litx_firmaNachName:text": "",
            "frm_suche:litx_vorname:text": "",
            "frm_suche:litx_sitzWohnsitz:text": "",
            "frm_suche:iaz_aktenzeichen:itx_abteilung": "",
            "frm_suche:iaz_aktenzeichen:som_registerzeichen:mysom": "NO_CODE",
            "frm_suche:iaz_aktenzeichen:itx_lfdNr": "",
            "frm_suche:iaz_aktenzeichen:itx_jahr": "",
            "frm_suche:iaz_aktenzeichen:ih_aktenzeichen": "true",
            "frm_suche:lsom_gegenstand:codelist:mysom": "NO_CODE",
            "frm_suche:ir_registereintrag:som_registergericht:mysom": "NO_CODE",
            "frm_suche:ir_registereintrag:som_registerart:mysom": "NO_CODE",
            "frm_suche:ir_registereintrag:itx_registernummer": "",
            "frm_suche:ir_registereintrag:ih_registereintrag": "true",
            "frm_suche:cbt_suchen": "Suchen",
            "jakarta.faces.ViewState": self.view_state,
        }

        url = f"{BASE_URL}{SEARCH_PATH};jsessionid={self.jsessionid}"
        log.info("Searching %s → %s ...", date_from, date_to)
        resp = self._request(
            "POST", url, data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if "zu viele Treffer" in resp.text:
            raise RuntimeError(
                f"Too many results for {date_from}→{date_to} (>1000)."
            )

        return self._parse_results(resp.text)

    def _parse_results(self, html: str) -> list[InsolvencyRecord]:
        soup = BeautifulSoup(html, "html.parser")
        vs = soup.find("input", {"name": "jakarta.faces.ViewState"})
        if vs and vs.get("value"):
            self.view_state = vs["value"]

        table = soup.find("table", id="tbl_ergebnis")
        if not table:
            log.warning("Result table tbl_ergebnis not found on page")
            return []

        records: list[InsolvencyRecord] = []
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 7:
                continue
            # Find the row index by extracting from span id
            sample_span = cells[0].find("span")
            if not sample_span:
                continue
            span_id = sample_span.get("id", "")
            m = re.match(r"tbl_ergebnis:(\d+):", span_id)
            if not m:
                continue
            row_index = int(m.group(1))

            # Find the button id within cell 6
            btn = cells[6].find("input", {"type": "image"})
            if not btn:
                continue
            button_id = btn.get("id")

            register_text = cells[5].get_text(" ", strip=True)
            reg_parsed = parse_register_info(register_text)

            rec = InsolvencyRecord(
                announcement_date=parse_german_date(cells[0].get_text(strip=True)),
                case_number=cells[1].get_text(strip=True) or None,
                court=cells[2].get_text(strip=True) or None,
                debtor_name=cells[3].get_text(" ", strip=True) or None,
                debtor_city=cells[4].get_text(strip=True) or None,
                register_info=register_text or None,
                registry_court=reg_parsed["registry_court"],
                registry_type=reg_parsed["registry_type"],
                registry_number=reg_parsed["registry_number"],
                # Page-level provenance: the public portal page where this
                # announcement is published/searchable. The JSF source exposes
                # no per-announcement GET permalink, so this is intentionally
                # page-level, not a fabricated unique deep link.
                source_url=f"{BASE_URL}{RESULT_PATH}",
                _row_index=row_index,
                _button_id=button_id,
            )
            records.append(rec)
        log.info("Parsed %d result rows", len(records))
        return records

    # ----- Detail fetch (serialized inside one session) ---------------------

    def fetch_detail_text(self, row_index: int, button_id: str) -> str:
        """Single AJAX call: the response itself contains the full announcement
        text inside <input id='frm_text:ihd_text' value='...'/>. This is more
        reliable than the secondary GET /ap/text.xhtml call (which depends on
        server-side state and can race under concurrency)."""
        with self._lock:
            ajax_payload = {
                f"tbl_ergebnis:{row_index}:frm_detail": f"tbl_ergebnis:{row_index}:frm_detail",
                button_id: "",
                "jakarta.faces.ViewState": self.view_state,
                "jakarta.faces.source": button_id,
                "jakarta.faces.partial.event": "click",
                "jakarta.faces.partial.execute": f"{button_id} @this",
                "jakarta.faces.partial.render": "msgs frm_text:ihd_text",
                "jakarta.faces.behavior.event": "click",
                "jakarta.faces.partial.ajax": "true",
            }
            headers = {
                "Faces-Request": "partial/ajax",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/xml, text/xml, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": f"{BASE_URL}{RESULT_PATH}",
            }
            resp = self._request("POST", f"{BASE_URL}{RESULT_PATH}", data=ajax_payload, headers=headers)

            # Extract text from the partial-response XML directly
            import html as html_module
            m = re.search(
                r'<input[^>]*id="frm_text:ihd_text"[^>]*value="([^"]*)"',
                resp.text,
                re.DOTALL,
            )
            if m:
                text = html_module.unescape(m.group(1))
                return re.sub(r"\n{3,}", "\n\n", text).strip()

            # Fallback: try secondary GET (legacy path)
            x = random.random()
            resp2 = self._request("GET", f"{BASE_URL}{TEXT_PATH}?x={x}")
            soup = BeautifulSoup(resp2.text, "html.parser")
            pre = soup.find("pre", id="veroefftext")
            if pre:
                text = pre.get_text("\n", strip=True)
                return re.sub(r"\n{3,}", "\n\n", text).strip()
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            container = soup.find(id="inhalt") or soup.body or soup
            text = container.get_text("\n", strip=True)
            return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------

class SupabaseClient:
    def __init__(self, url: str, service_key: str, run_id: str) -> None:
        self.url = url.rstrip("/")
        self.service_key = service_key
        self.run_id = run_id
        self.base_headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
        }

    @staticmethod
    def _stable_key_from_row(r: dict) -> tuple:
        """Build the stable identity tuple from a DB row, matching
        InsolvencyRecord.unique_key() (announcement_type_hint excluded).

        Applies the same ``_norm_key_val`` normalization as unique_key() so a
        value stored as ``''`` or with stray whitespace matches a scraper-side
        ``None``/trimmed value."""
        return tuple(
            _norm_key_val(v) for v in (
                r.get("court"), r.get("case_number"), r.get("announcement_date"),
                r.get("registry_court"), r.get("registry_type"),
                r.get("registry_number"),
            )
        )

    def get_existing_keys(
        self, date_from: date, date_to: date, max_attempts: int
    ) -> tuple[set[tuple], set[tuple], dict[tuple, str]]:
        """Return existing-row state for the date range.

        Returns ``(existing_keys, empty_text_keys, empty_text_ids)`` where:
          - ``existing_keys``   : stable keys already present in the DB.
          - ``empty_text_keys`` : subset of those whose ``announcement_text`` is
            NULL/empty AND ``detail_fetch_attempts < max_attempts`` — i.e. rows
            still worth a detail-fetch. Rows that have already been attempted
            ``max_attempts`` times (server keeps returning empty) are excluded so
            they cannot livelock the backfill set.
          - ``empty_text_ids``  : ``key -> db id`` for the empty_text_keys, so the
            caller can bump ``detail_fetch_attempts`` by id after fetching.

        The key tuple mirrors InsolvencyRecord.unique_key() exactly so that
        ``record.unique_key() in existing_keys`` matches reliably."""
        select = (
            "id,court,case_number,announcement_date,registry_court,"
            "registry_type,registry_number,announcement_text,detail_fetch_attempts"
        )
        endpoint = (
            f"{self.url}/rest/v1/apify_cases?select={select}"
            f"&announcement_date=gte.{date_from.isoformat()}"
            f"&announcement_date=lte.{date_to.isoformat()}"
        )
        resp = requests.get(endpoint, headers=self.base_headers, timeout=60)
        if resp.status_code >= 400:
            log.warning("Failed to fetch existing keys: %d %s", resp.status_code, resp.text[:200])
            return set(), set(), {}
        rows = resp.json() or []
        existing_keys: set[tuple] = set()
        empty_text_keys: set[tuple] = set()
        empty_text_ids: dict[tuple, str] = {}
        for r in rows:
            key = self._stable_key_from_row(r)
            existing_keys.add(key)
            text = r.get("announcement_text")
            attempts = r.get("detail_fetch_attempts") or 0
            if (text is None or not str(text).strip()) and attempts < max_attempts:
                empty_text_keys.add(key)
                if r.get("id") is not None:
                    empty_text_ids[key] = r["id"]
        return existing_keys, empty_text_keys, empty_text_ids

    def fetch_empty_text_backlog(
        self, max_attempts: int, limit: int
    ) -> list[dict]:
        """Directly select empty-text rows from the DB, independent of the
        current listing/scrape window (BUG 1: rows outside today's window were
        otherwise unreachable). Oldest-first so the longest-waiting rows drain
        first, capped at ``limit`` (the reserved backfill budget).

        Returns the raw DB rows (dicts) including ``id`` and the identity fields
        needed to re-locate the row in a fresh listing search.

        BUG 2 (fixed here): PostgREST enforces a *server-side* ``max-rows``
        ceiling (1000 on this project). A single ``Range``/``?limit=`` request
        CANNOT exceed it — ``Range`` only raises the per-request page up to
        ``max-rows``, it does not disable the cap. The previous one-shot fetch
        therefore silently saw only the oldest 1000 empty rows every run, so any
        backlog larger than 1000 (we have ~9k) could never fully drain: the same
        oldest slice was re-served each run until those rows hit
        ``max_attempts`` and dropped out. We now PAGINATE with successive
        ``Range`` requests (each bounded by max-rows) until we have ``limit``
        empty-text rows or the backlog is exhausted."""
        if limit <= 0:
            return []
        select = (
            "id,court,case_number,announcement_date,registry_court,"
            "registry_type,registry_number,announcement_text,detail_fetch_attempts"
        )
        # Deterministic order (date, then id) so offset paging can neither skip
        # nor duplicate rows across pages. Server-side NULL-text + attempts-cap
        # predicates keep each page cheap; the client-side trim guard below is a
        # belt-and-braces check (is.null already excludes empty strings).
        base = (
            f"{self.url}/rest/v1/apify_cases?select={select}"
            f"&detail_fetch_attempts=lt.{max_attempts}"
            f"&announcement_text=is.null"
            f"&order=announcement_date.asc,id.asc"
        )
        page_size = 1000  # PostgREST max-rows on this project = one full page
        max_pages = 50    # hard safety bound (<=50k rows scanned) → always terminates
        out: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            headers = dict(self.base_headers)
            headers["Range-Unit"] = "items"
            headers["Range"] = f"{offset}-{offset + page_size - 1}"
            resp = requests.get(base, headers=headers, timeout=120)
            if resp.status_code >= 400 and resp.status_code != 206:
                log.warning("Failed to fetch empty-text backlog (offset %d): %d %s",
                            offset, resp.status_code, resp.text[:200])
                break
            rows = resp.json() or []
            if not rows:
                break
            for r in rows:
                text = r.get("announcement_text")
                if text is None or not str(text).strip():
                    out.append(r)
                    if len(out) >= limit:
                        break
            if len(out) >= limit or len(rows) < page_size:
                break  # have enough, or this was the last (short) page → exhausted
            offset += page_size
        log.info("fetch_empty_text_backlog: collected %d empty-text rows "
                 "(limit=%d) across paginated fetch", len(out), limit)
        return out[:limit]

    def resolve_ids_by_key(
        self, records: list["InsolvencyRecord"]
    ) -> dict[tuple, str]:
        """Resolve the DB ``id`` for each attempted record by its stable
        ``unique_key()``, independent of whether ``_db_id`` was ever populated.

        This is the linchpin of reliable attempt-tracking: the old by-id bump
        only fired for rows whose ``_db_id`` happened to be set (in-window empty
        rows and recovered backlog rows), so brand-new rows — and any row whose
        ``empty_text_ids`` lookup missed — were never counted. Here we re-query
        the DB over the exact announcement_date(s) we attempted and match each
        returned row to an attempted record via the SAME normalized stable key
        used everywhere else (``_stable_key_from_row`` mirrors ``unique_key``).
        Because this runs AFTER the upsert, even rows that were brand-new this
        run now exist and resolve to a real id.

        Returns a ``{stable_key_tuple -> db id}`` map."""
        if not records:
            return {}
        # Collect the distinct dates we need to scan. A missing/empty date is
        # rare for existing rows but we guard it: such rows simply won't resolve
        # (and are logged by the caller's read-back mismatch warning).
        dates: set[date] = set()
        for r in records:
            ad = r.announcement_date
            if ad:
                try:
                    dates.add(date.fromisoformat(str(ad)[:10]))
                except ValueError:
                    continue
        wanted: set[tuple] = {r.unique_key() for r in records}
        key_to_id: dict[tuple, str] = {}
        select = (
            "id,court,case_number,announcement_date,registry_court,"
            "registry_type,registry_number"
        )
        for d in sorted(dates):
            endpoint = (
                f"{self.url}/rest/v1/apify_cases?select={select}"
                f"&announcement_date=eq.{d.isoformat()}"
            )
            try:
                resp = requests.get(endpoint, headers=self.base_headers, timeout=60)
            except requests.RequestException as e:
                log.warning("resolve_ids_by_key request error for %s: %s", d, e)
                continue
            if resp.status_code >= 400:
                log.warning("resolve_ids_by_key read failed for %s: %d %s",
                            d, resp.status_code, resp.text[:200])
                continue
            for row in (resp.json() or []):
                key = self._stable_key_from_row(row)
                if key in wanted and row.get("id") is not None:
                    key_to_id[key] = row["id"]
        return key_to_id

    def bump_detail_attempts_for_records(
        self, records: list["InsolvencyRecord"]
    ) -> int:
        """Increment ``detail_fetch_attempts`` (+1) and stamp
        ``last_detail_attempt_at`` for every record we attempted a detail-fetch
        on this run — success OR empty result — keyed by the row's stable
        ``unique_key()`` rather than a pre-resolved ``_db_id``.

        This is the fix for the production bug where ALL rows kept
        ``detail_fetch_attempts = 0``: the previous by-id path (a) only ran for
        rows with ``_db_id`` set, and (b) wrote via a partial-row upsert
        (``on_conflict=id`` + ``merge-duplicates``) that PostgREST evaluates as an
        INSERT-first statement — the partial payload omits NOT-NULL columns, so
        the write silently failed and was swallowed as a warning. Here we instead
        resolve real ids by key (after the upsert, so every attempted row exists)
        and issue a real PATCH per id, then READ BACK to verify the count.

        Returns the number of rows actually updated (read-back count), so a future
        silent failure is visible in the logs."""
        if not records:
            return 0
        key_to_id = self.resolve_ids_by_key(records)
        ids = sorted(set(key_to_id.values()))
        attempted = len({r.unique_key() for r in records})
        if not ids:
            log.warning(
                "bump_detail_attempts: resolved 0 ids for %d attempted rows — "
                "attempt-tracking did NOT persist (check key normalization).",
                attempted)
            return 0
        updated = self._patch_attempts_by_id(ids)
        if updated != len(ids):
            log.warning(
                "bump_detail_attempts: expected to update %d rows but DB "
                "read-back shows %d updated this run (attempted=%d, resolved=%d).",
                len(ids), updated, attempted, len(ids))
        else:
            log.info(
                "bump_detail_attempts: persisted +1 attempt for %d/%d rows "
                "(verified by read-back).", updated, attempted)
        return updated

    def _patch_attempts_by_id(self, ids: list[str]) -> int:
        """Real per-id PATCH (NOT an upsert): read current attempts, write
        ``value + 1`` and a fresh ``last_detail_attempt_at`` via HTTP PATCH with
        an ``id=eq.<id>`` filter, then read back and count how many rows now carry
        this run's timestamp. PostgREST has no atomic ``col = col + 1``, so the
        read-then-write is per-id; ids are UUIDs stored as text and a single-id
        ``eq`` filter sidesteps any ``in.()`` quoting ambiguity.

        Returns the verified count of rows updated with this run's timestamp."""
        if not ids:
            return 0
        unique_ids = sorted(set(ids))
        id_csv = ",".join(str(i) for i in unique_ids)
        now_iso = datetime.now(timezone.utc).isoformat()
        # 1) Read current attempt counts for all touched ids in one request.
        read_ep = (
            f"{self.url}/rest/v1/apify_cases"
            f"?select=id,detail_fetch_attempts&id=in.({id_csv})"
        )
        try:
            resp = requests.get(read_ep, headers=self.base_headers, timeout=60)
        except requests.RequestException as e:
            log.warning("bump_detail_attempts read error: %s", e)
            return 0
        if resp.status_code >= 400:
            log.warning("bump_detail_attempts read failed: %d %s",
                        resp.status_code, resp.text[:200])
            return 0
        current = {row["id"]: (row.get("detail_fetch_attempts") or 0)
                   for row in (resp.json() or [])}
        # 2) PATCH each id with value+1 and the run timestamp. A real PATCH only
        #    UPDATEs existing rows (no INSERT branch, so no NOT-NULL surprise).
        patch_headers = {
            **self.base_headers,
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        for i in unique_ids:
            body = {
                "detail_fetch_attempts": current.get(i, 0) + 1,
                "last_detail_attempt_at": now_iso,
            }
            patch_ep = f"{self.url}/rest/v1/apify_cases?id=eq.{i}"
            try:
                presp = requests.patch(
                    patch_ep, json=body, headers=patch_headers, timeout=60)
                if presp.status_code >= 400:
                    log.warning("bump_detail_attempts PATCH failed for %s: %d %s",
                                i, presp.status_code, presp.text[:200])
            except requests.RequestException as e:
                log.warning("bump_detail_attempts PATCH error for %s: %s", i, e)
        # 3) Read back: count rows whose last_detail_attempt_at == this run's
        #    timestamp, proving the write actually landed. The ISO timestamp
        #    contains a literal '+' ('+00:00') which a URL query parser decodes as
        #    a space, so it MUST be percent-encoded or the filter silently matches
        #    nothing (and the verify would wrongly report 0).
        verify_ep = (
            f"{self.url}/rest/v1/apify_cases"
            f"?select=id&id=in.({id_csv})"
            f"&last_detail_attempt_at=eq.{quote(now_iso, safe='')}"
        )
        try:
            vresp = requests.get(verify_ep, headers=self.base_headers, timeout=60)
            if vresp.status_code >= 400:
                log.warning("bump_detail_attempts verify read failed: %d %s",
                            vresp.status_code, vresp.text[:200])
                return 0
            return len(vresp.json() or [])
        except requests.RequestException as e:
            log.warning("bump_detail_attempts verify error: %s", e)
            return 0

    # PostgreSQL duplicate-key SQLSTATE returned by PostgREST as JSON "code".
    # Treated as a *non-fatal* conflict at chunk level: we retry the chunk
    # row-by-row and only count the actually-conflicting rows.
    _PG_UNIQUE_VIOLATION = "23505"

    @staticmethod
    def _is_duplicate_key_error(status_code: int, body_text: str) -> bool:
        """Detect a PostgREST duplicate-key error reliably.

        We require BOTH the HTTP 409 status *and* the PG SQLSTATE 23505 in
        the body, so that other 409s (e.g. concurrent-update conflicts) are
        still treated as fatal until we explicitly classify them.
        """
        if status_code != 409:
            return False
        try:
            payload = json.loads(body_text)
        except (json.JSONDecodeError, ValueError):
            return False
        if isinstance(payload, dict):
            return payload.get("code") == SupabaseClient._PG_UNIQUE_VIOLATION
        return False

    def _post_rpc(self, endpoint: str, headers: dict, payload: list[dict],
                  timeout: int) -> tuple[int, str]:
        """Single RPC call. Returns (status_code, response_text). Network
        exceptions are re-raised so callers can decide how to count them."""
        resp = requests.post(
            endpoint,
            json={"p_records": payload},
            headers=headers,
            timeout=timeout,
        )
        return resp.status_code, resp.text

    @staticmethod
    def _parse_counts(body_text: str) -> tuple[int, int]:
        """Extract (rows_inserted, rows_updated) from a successful RPC body."""
        try:
            body = json.loads(body_text)
        except (json.JSONDecodeError, ValueError):
            return 0, 0
        if isinstance(body, list) and body and isinstance(body[0], dict):
            row = body[0]
        elif isinstance(body, dict):
            row = body
        else:
            return 0, 0
        return int(row.get("rows_inserted", 0)), int(row.get("rows_updated", 0))

    def upsert_cases(self, records: list[InsolvencyRecord]) -> dict:
        """Calls the public.neu_insolvenz_fill_only_upsert(jsonb) RPC.

        Behavior
        --------
        - Sends records in chunks (default 100).
        - On chunk **success** (HTTP 2xx): parses ``rows_inserted`` and
          ``rows_updated`` from the RPC's TABLE return.
        - On chunk **HTTP 409 duplicate-key** (PG SQLSTATE 23505): the whole
          chunk is poisoned by even one conflicting row, so we re-send the
          chunk row-by-row and only count the genuinely-conflicting rows as
          ``duplicates_skipped``. Non-conflicting rows in the chunk still
          get inserted/updated normally.
        - On any **other 4xx/5xx** or network exception: counted as
          ``real_errors``. Those still cause the run to fail at the caller.

        Returns
        -------
        A dict with the following keys:
            inserted_total, updated_total, duplicates_skipped, real_errors,
            total_payload_records, chunks_total, chunks_conflict_retried.

        Fill-only semantics are unchanged — they live entirely in the RPC.
        """
        result = {
            "inserted_total": 0,
            "updated_total": 0,
            "duplicates_skipped": 0,
            "real_errors": 0,
            "total_payload_records": 0,
            "chunks_total": 0,
            "chunks_conflict_retried": 0,
        }
        if not records:
            return result

        for r in records:
            r.source_run_id = self.run_id

        endpoint = f"{self.url}/rest/v1/rpc/neu_insolvenz_fill_only_upsert"
        headers = {
            **self.base_headers,
            "Content-Type": "application/json",
        }
        payload_all = [r.to_db_dict() for r in records]
        result["total_payload_records"] = len(payload_all)

        CHUNK = 100
        for i in range(0, len(payload_all), CHUNK):
            chunk = payload_all[i:i + CHUNK]
            result["chunks_total"] += 1
            try:
                status, body_text = self._post_rpc(endpoint, headers, chunk, timeout=120)
            except requests.RequestException as e:
                log.error("Upsert exception @%d: %s", i, e)
                result["real_errors"] += len(chunk)
                continue

            if 200 <= status < 300:
                ins, upd = self._parse_counts(body_text)
                result["inserted_total"] += ins
                result["updated_total"] += upd
                continue

            if self._is_duplicate_key_error(status, body_text):
                # One bad row poisoned the whole 100-row chunk. Retry per-row
                # so the other 99+ rows can land and we count the conflict
                # precisely.
                result["chunks_conflict_retried"] += 1
                log.warning(
                    "Upsert chunk @%d hit duplicate-key (HTTP 409 / SQLSTATE 23505). "
                    "Retrying %d rows one-by-one.",
                    i, len(chunk),
                )
                self._retry_single_rows(endpoint, headers, chunk, result, base_index=i)
                continue

            # Anything else: real failure.
            log.error(
                "Upsert RPC failed @%d: %d %s",
                i, status, body_text[:500],
            )
            result["real_errors"] += len(chunk)

        log.info(
            "Upsert summary: inserted=%d updated=%d duplicates_skipped=%d "
            "real_errors=%d (total=%d, chunks=%d, conflict_retries=%d)",
            result["inserted_total"], result["updated_total"],
            result["duplicates_skipped"], result["real_errors"],
            result["total_payload_records"], result["chunks_total"],
            result["chunks_conflict_retried"],
        )
        return result

    def _retry_single_rows(self, endpoint: str, headers: dict,
                            chunk: list[dict], result: dict,
                            base_index: int) -> None:
        """Retry a 409-poisoned chunk one row at a time.

        For each row:
          - 2xx                          -> counted as inserted/updated
          - 409 SQLSTATE 23505           -> counted as duplicates_skipped (non-fatal)
          - other 4xx/5xx or network err -> counted as real_errors
        """
        for offset, row in enumerate(chunk):
            try:
                status, body_text = self._post_rpc(endpoint, headers, [row], timeout=60)
            except requests.RequestException as e:
                log.error("Single-row retry exception @%d: %s", base_index + offset, e)
                result["real_errors"] += 1
                continue

            if 200 <= status < 300:
                ins, upd = self._parse_counts(body_text)
                result["inserted_total"] += ins
                result["updated_total"] += upd
                continue

            if self._is_duplicate_key_error(status, body_text):
                result["duplicates_skipped"] += 1
                # Keep this at INFO level; the chunk warning above is the
                # loud one. A compact row identifier keeps the daily log
                # auditable without flooding it.
                key = (
                    row.get("court"), row.get("case_number"),
                    row.get("announcement_date"),
                    row.get("announcement_type_hint"),
                )
                log.info("  duplicate-key skipped: %s", key)
                continue

            log.error(
                "Single-row retry failed @%d: %d %s",
                base_index + offset, status, body_text[:300],
            )
            result["real_errors"] += 1


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def recover_backlog_records(backlog_rows: list[dict]) -> list[InsolvencyRecord]:
    """Turn DB backlog rows (from ``fetch_empty_text_backlog``) into fetchable
    ``InsolvencyRecord`` objects, independent of the current scrape window.

    A detail-fetch needs the session-specific ``_row_index``/``_button_id``,
    which only exist after a listing search. So we search ONCE per distinct
    ``announcement_date`` present in the backlog, match each returned listing row
    to its DB row by the stable ``unique_key()``, and copy over the recovered
    row index/button id plus the DB ``id`` (so attempts can be bumped later).

    Rows whose date can no longer be searched, or that no longer appear in the
    portal's results, are skipped (they will simply remain in the backlog until
    they age past MAX_DETAIL_ATTEMPTS once attempted, or stay unreachable — a
    limitation documented in the README)."""
    if not backlog_rows:
        return []

    # Map stable key -> db row, and collect distinct dates to search.
    by_key: dict[tuple, dict] = {}
    dates: set[date] = set()
    for r in backlog_rows:
        key = SupabaseClient._stable_key_from_row(r)
        by_key[key] = r
        ad = r.get("announcement_date")
        if ad:
            try:
                dates.add(date.fromisoformat(str(ad)[:10]))
            except ValueError:
                continue

    recovered: list[InsolvencyRecord] = []
    for d in sorted(dates):  # oldest-first
        try:
            s = NeuInsolvenzScraper()
            s.initialize_session()
            day_listing = s.search(d, d)
        except Exception as e:  # noqa: BLE001 — one bad day must not abort backfill
            log.warning("Backlog recovery search failed for %s: %s", d, e)
            continue
        for rec in day_listing:
            row = by_key.get(rec.unique_key())
            if row is None:
                continue
            rec._db_id = row.get("id")
            recovered.append(rec)
    log.info("Backlog recovery: matched %d / %d empty-text rows via date searches",
             len(recovered), len(backlog_rows))
    return recovered


def fetch_details_with_pool(
    listing: list[InsolvencyRecord],
    workers: int,
) -> int:
    """For each record in `listing`, fetch its full text and enrich the record.
    Uses `workers` parallel JSF sessions (each session serializes its own
    detail fetches because of the server-side last-clicked-row state).

    Mutates each record in place (announcement_text + extracted fields). Returns
    the number of records whose detail-fetch *succeeded* (text was retrieved)."""
    if not listing or workers < 1:
        return 0

    # Split listing across workers
    chunks: list[list[InsolvencyRecord]] = [[] for _ in range(workers)]
    for i, rec in enumerate(listing):
        chunks[i % workers].append(rec)

    total_done = 0
    done_lock = Lock()

    def run_worker(worker_id: int, work_items: list[InsolvencyRecord]) -> int:
        if not work_items:
            return 0
        # IMPORTANT: each worker needs its own session AND must run its own
        # search to populate that session's results state (because the
        # row-index → record mapping is session-specific).
        #
        # The row index (`rec._row_index`) is parsed positionally from
        # `tbl_ergebnis:<N>:` ids within a SINGLE search result page (see
        # _parse_results). It is only valid for the exact (single-day) search
        # that produced it. A worker chunk can mix records from several distinct
        # announcement dates (backlog/out-of-window rows get round-robined across
        # workers), so we must NEVER search a multi-day range here: a wide range
        # both invalidates the positional indices AND can trip the source's
        # "zu viele Treffer" guard (>1000 rows), which raises in search().
        #
        # So: group work by announcement_date and run one SINGLE-DAY search per
        # date (date_from == date_to), then fetch only that day's records before
        # moving on — mirroring the main per-day listing loop that produced the
        # indices in the first place.
        worker = NeuInsolvenzScraper()
        worker.initialize_session()

        # Group this chunk's records by their announcement date. Records with a
        # missing/empty date can't be re-searched safely (no day to scope to);
        # bucket them under today's date as a best-effort single-day search
        # rather than ever widening into a range.
        by_date: dict[date, list[InsolvencyRecord]] = {}
        for it in work_items:
            if it.announcement_date:
                try:
                    d = date.fromisoformat(it.announcement_date)
                except ValueError:
                    log.warning(
                        "Worker %d: unparseable announcement_date %r (az=%s) — "
                        "using today() for single-day re-search",
                        worker_id, it.announcement_date, it.case_number)
                    d = date.today()
            else:
                log.warning(
                    "Worker %d: record has no announcement_date (az=%s) — "
                    "using today() for single-day re-search",
                    worker_id, it.case_number)
                d = date.today()
            by_date.setdefault(d, []).append(it)

        done = 0
        for d in sorted(by_date):
            day_items = by_date[d]
            # Isolate each date: a transient error or an unexpected "zu viele
            # Treffer" for one day must NOT abort the other dates or the run.
            try:
                worker.search(d, d)  # single-day range only — never a span
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "Worker %d: re-search for %s failed (%d records skipped): %s",
                    worker_id, d, len(day_items), e)
                continue

            for rec in day_items:
                try:
                    text = worker.fetch_detail_text(rec._row_index, rec._button_id)
                    rec.announcement_text = text
                    fields = extract_fields_from_text(text)
                    rec.insolvency_administrator = fields["insolvency_administrator"]
                    rec.opening_date = fields["opening_date"]
                    rec.claims_deadline = fields["claims_deadline"]
                    rec.announcement_type_hint = fields["announcement_type_hint"]
                    rec.announcement_type_raw = fields["announcement_type_raw"]
                    rec.insolvency_phase = fields["insolvency_phase"]
                    rec.is_pre_verteilung = fields["is_pre_verteilung"]
                    done += 1
                    with done_lock:
                        nonlocal total_done
                        total_done += 1
                        if total_done % 50 == 0:
                            log.info("Detail progress: %d/%d", total_done, len(listing))
                    time.sleep(DETAIL_FETCH_DELAY_SEC)
                except Exception as e:
                    log.warning("Detail fetch failed (worker=%d, date=%s, row=%d, az=%s): %s",
                                worker_id, d, rec._row_index, rec.case_number, e)
        return done

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_worker, i, chunk) for i, chunk in enumerate(chunks)]
        for f in as_completed(futures):
            # A worker should already isolate its own per-date/per-record
            # failures, but never let an unexpected exception from one worker
            # abort the whole run — log it and keep collecting the rest.
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                log.warning("A detail-fetch worker raised and was isolated: %s", e)
    return total_done


# ---------------------------------------------------------------------------
# Run diagnostics / guardrails (Phase 0037D)
# ---------------------------------------------------------------------------

# Warn (and optionally fail) when extraction quality regresses. A textless row
# legitimately has no type, so the type-coverage guard is measured ONLY over
# rows that DO have announcement_text (a classifier-quality signal), not over
# every parsed row (most rows carry no text by design on a budget-capped run).
TYPE_COVERAGE_WARN_RATIO = 0.20   # warn if >20% of text-bearing rows lack a type
URL_COVERAGE_WARN_RATIO = 0.20    # warn if >20% of rows lack a source_url
# Hard-fail is OFF by default so a normal day (where most rows have no detail
# text yet) cannot break production. Set FAIL_ON_LOW_COVERAGE=1 to escalate the
# warnings below into a non-zero exit (e.g. in a dedicated QA workflow).
FAIL_ON_LOW_COVERAGE = os.getenv("FAIL_ON_LOW_COVERAGE", "0") == "1"


def _nonempty(v: Any) -> bool:
    return v is not None and str(v).strip() != ""


def compute_coverage_metrics(records: list[InsolvencyRecord]) -> dict[str, int]:
    """Non-sensitive run diagnostics (counts only — never debtor names or text).

    Returns counts used both for logging and for the coverage guardrails."""
    total = len(records)
    with_text = sum(1 for r in records if _nonempty(r.announcement_text))
    with_type = sum(1 for r in records if _nonempty(r.announcement_type_hint))
    with_url = sum(1 for r in records if _nonempty(r.source_url))
    with_pub = sum(1 for r in records if _nonempty(r.insolvenzindex_publication_id))
    with_hash = sum(1 for r in records if _nonempty(r.content_hash))
    text_no_type = sum(
        1 for r in records
        if _nonempty(r.announcement_text) and not _nonempty(r.announcement_type_hint)
    )
    return {
        "total": total,
        "with_text": with_text,
        "missing_text": total - with_text,
        "with_type": with_type,
        "missing_type": total - with_type,
        "with_source_url": with_url,
        "with_publication_id": with_pub,
        "with_content_hash": with_hash,
        "text_but_no_type": text_no_type,
    }


def log_coverage_and_guardrails(records: list[InsolvencyRecord]) -> bool:
    """Emit non-sensitive coverage diagnostics and evaluate the guardrails.

    Returns True if a guardrail tripped (caller decides whether to fail). Never
    logs debtor names or announcement text — counts only."""
    # content_hash is computed lazily in to_db_dict(); make sure it exists for
    # the metric (does not mutate the DB payload path).
    for r in records:
        if not _nonempty(r.content_hash):
            r.content_hash = r.compute_content_hash()
    m = compute_coverage_metrics(records)
    log.info(
        "Run diagnostics: parsed=%d | text=%d (missing=%d) | type_hint=%d "
        "(missing=%d) | source_url=%d | publication_id=%d | content_hash=%d",
        m["total"], m["with_text"], m["missing_text"], m["with_type"],
        m["missing_type"], m["with_source_url"], m["with_publication_id"],
        m["with_content_hash"],
    )
    tripped = False
    if m["with_text"] > 0:
        ratio = m["text_but_no_type"] / m["with_text"]
        if ratio > TYPE_COVERAGE_WARN_RATIO:
            tripped = True
            log.warning(
                "GUARDRAIL: %d/%d (%.1f%%) of text-bearing rows have NO "
                "announcement_type_hint (threshold %.0f%%) — classifier gap or "
                "unexpected announcement wording. Investigate TYPE_KEYWORDS.",
                m["text_but_no_type"], m["with_text"], 100.0 * ratio,
                100.0 * TYPE_COVERAGE_WARN_RATIO,
            )
    if m["total"] > 0:
        missing_url_ratio = (m["total"] - m["with_source_url"]) / m["total"]
        if missing_url_ratio > URL_COVERAGE_WARN_RATIO:
            tripped = True
            log.warning(
                "GUARDRAIL: %d/%d (%.1f%%) of rows lack a source_url "
                "(threshold %.0f%%) — provenance not captured.",
                m["total"] - m["with_source_url"], m["total"],
                100.0 * missing_url_ratio, 100.0 * URL_COVERAGE_WARN_RATIO,
            )
    return tripped


# Fields that may carry natural-person identity or full announcement bodies and
# must never be printed in full to logs (privacy). Shown as redacted summaries.
_SENSITIVE_LOG_FIELDS = {
    "debtor_name", "debtor_city", "announcement_text", "external_raw",
    "insolvency_administrator", "register_info",
}


def _redact_for_log(key: str, value: Any) -> Any:
    """Privacy-safe rendering of a record field for DRY_RUN/sample logging.

    Natural persons make up the bulk of the source, so debtor identity and the
    full announcement body are NEVER emitted — only a redacted length/type
    summary. Non-sensitive structured fields are shown (truncated)."""
    if value is None:
        return None
    if key in _SENSITIVE_LOG_FIELDS:
        if isinstance(value, str):
            return f"<redacted:{len(value)} chars>"
        return "<redacted>"
    s = str(value)
    return (s[:140] + "...") if len(s) > 140 else value


def main() -> int:
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    dry_run = os.getenv("DRY_RUN") == "1"
    skip_details = os.getenv("SKIP_DETAILS") == "1"
    max_details = int(os.getenv("MAX_DETAIL_FETCHES", str(DEFAULT_MAX_DETAIL_FETCHES)))
    max_attempts = int(os.getenv("MAX_DETAIL_ATTEMPTS", str(DEFAULT_MAX_DETAIL_ATTEMPTS)))
    workers = int(os.getenv("DETAIL_WORKERS", str(DEFAULT_DETAIL_WORKERS)))

    if not dry_run and (not supabase_url or not supabase_key):
        log.error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required (or set DRY_RUN=1)")
        return 2

    today = date.today()
    date_from_str = os.getenv("SCRAPE_DATE_FROM") or today.isoformat()
    date_to_str = os.getenv("SCRAPE_DATE_TO") or today.isoformat()
    try:
        date_from = date.fromisoformat(date_from_str)
        date_to = date.fromisoformat(date_to_str)
    except ValueError as e:
        log.error("Invalid date format (use YYYY-MM-DD): %s", e)
        return 2

    run_id = f"gha-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    log.info("=== Run start: id=%s | %s → %s | dry=%s | skip_details=%s | max_details=%d | workers=%d ===",
             run_id, date_from, date_to, dry_run, skip_details, max_details, workers)

    # Phase 1: listing
    scraper = NeuInsolvenzScraper()
    scraper.initialize_session()
    listing = scraper.search(date_from, date_to)
    if not listing:
        log.warning("No results in range. Done.")
        return 0

    # Phase 2: detail fetching.
    # We detail-fetch three groups, in priority order:
    #   (a) backfill_records — existing DB rows with empty/NULL announcement_text
    #       and detail_fetch_attempts < MAX_DETAIL_ATTEMPTS. These come from BOTH
    #       the current listing window AND a direct-by-id DB query (so rows
    #       outside today's scrape window are still reachable — BUG 1). Oldest
    #       first, so the longest-waiting rows drain first.
    #   (b) new_records      — rows not yet in the DB.
    # The backfill group can never take more than half of MAX_DETAIL_FETCHES, so
    # a large/stuck backlog can never fully starve brand-new rows. Every detail
    # attempt on an existing row bumps detail_fetch_attempts so rows that keep
    # returning empty text drop out of the candidate set after the cap.
    new_records: list[InsolvencyRecord] = listing
    backfill_records: list[InsolvencyRecord] = []
    client: SupabaseClient | None = None
    if not dry_run and supabase_url and supabase_key:
        client = SupabaseClient(supabase_url, supabase_key, run_id)
        existing_keys, empty_text_keys, empty_text_ids = client.get_existing_keys(
            date_from, date_to, max_attempts)
        log.info("Existing DB keys in range: %d (of which empty announcement_text & retryable: %d)",
                 len(existing_keys), len(empty_text_keys))
        new_records = [r for r in listing if r.unique_key() not in existing_keys]
        # In-window backfill: listing rows whose DB row is empty + retryable.
        # (Attempt-tracking is now resolved by unique_key() after the upsert via
        # bump_detail_attempts_for_records, so we no longer depend on _db_id.)
        backfill_records = []
        seen_backfill: set[tuple] = set()
        for r in listing:
            k = r.unique_key()
            if k in empty_text_keys:
                backfill_records.append(r)
                seen_backfill.add(k)
        log.info("New (not-yet-in-DB) records: %d / %d total listing rows",
                 len(new_records), len(listing))
        log.info("In-window rows needing announcement_text backfill: %d", len(backfill_records))

        # Direct-by-id backlog: empty-text rows from anywhere in the DB (not just
        # today's window), oldest-first, capped at the reserved backfill budget.
        backfill_budget = max(1, max_details // 2)
        backlog_rows = client.fetch_empty_text_backlog(max_attempts, backfill_budget)
        if backlog_rows:
            recovered = recover_backlog_records(backlog_rows)
            for rec in recovered:
                if rec.unique_key() not in seen_backfill:
                    backfill_records.append(rec)
                    seen_backfill.add(rec.unique_key())
            log.info("Out-of-window backlog rows added for backfill: %d", len(recovered))
    else:
        log.info("Dry run — skipping DB key check.")

    # Records that came from the direct-by-id backlog are NOT part of `listing`,
    # so they must be persisted explicitly in Phase 3.
    extra_persist: list[InsolvencyRecord] = [
        r for r in backfill_records if r not in listing
    ]

    # Records we actually attempt a detail-fetch on this run (any group). Their
    # detail_fetch_attempts must be incremented by exactly 1 AFTER the upsert
    # (so brand-new rows already exist and resolve to a real id).
    to_fetch: list[InsolvencyRecord] = []
    if skip_details:
        log.info("SKIP_DETAILS=1 — not fetching announcement_text.")
    else:
        # Oldest-first within each group so the longest-waiting rows drain first
        # (aligns with the README). Reserve at least half of MAX_DETAIL_FETCHES
        # for NEW rows so backfill can never 100% starve them.
        def _by_date_asc(recs: list[InsolvencyRecord]) -> list[InsolvencyRecord]:
            return sorted(recs, key=lambda r: r.announcement_date or "")

        backfill_budget = max(1, max_details // 2)
        backfill_part = _by_date_asc(backfill_records)[:backfill_budget]
        ordered = backfill_part + _by_date_asc(new_records)
        to_fetch = ordered[:max_details]
        if len(ordered) > max_details:
            log.warning(
                "Capped detail fetches at %d (had %d to fetch: %d backfill (budget %d) + %d new). "
                "Remaining backlog will be picked up on the next run.",
                max_details, len(ordered), len(backfill_records), backfill_budget,
                len(new_records))
        if to_fetch:
            log.info("Fetching details for %d records with %d workers...",
                     len(to_fetch), workers)
            t0 = time.time()
            fetched = fetch_details_with_pool(to_fetch, workers)
            elapsed = time.time() - t0
            log.info("Detail fetches done: %d/%d in %.1fs (%.2fs avg)",
                     fetched, len(to_fetch), elapsed, elapsed / max(1, fetched))

    # Coverage diagnostics + guardrails over everything we are about to persist
    # (counts only — privacy-safe). Computed for both dry-run and real runs.
    persist_set = listing + extra_persist
    guardrail_tripped = log_coverage_and_guardrails(persist_set)

    # Phase 3: persist
    if dry_run:
        log.info("DRY_RUN=1 — printing REDACTED sample record and exiting.")
        if listing:
            sample = next((r for r in listing if r.announcement_text), listing[0])
            for k, v in sample.to_db_dict().items():
                log.info("  %s = %r", k, _redact_for_log(k, v))
            # Always surface the computed phase classification in dry-run, even
            # when WRITE_PHASE_FIELDS is off (so they are excluded from to_db_dict).
            log.info("  insolvency_phase = %r", sample.insolvency_phase)
            log.info("  is_pre_verteilung = %r", sample.is_pre_verteilung)
        return 0

    if client is None:
        client = SupabaseClient(supabase_url, supabase_key, run_id)
    # Upsert all listing rows plus any out-of-window backlog rows we backfilled
    # (fill-only on existing thanks to the RPC).
    stats = client.upsert_cases(listing + extra_persist)

    # Persist detail_fetch_attempts for every row we attempted this run — keyed
    # by the row's stable unique_key(), NOT by _db_id. Must run AFTER the upsert
    # so brand-new rows already exist in the DB and resolve to a real id. Rows
    # NOT attempted this run are left untouched. This is what makes the
    # MAX_DETAIL_ATTEMPTS cap actually engage: empty-at-source rows accumulate
    # attempts and eventually drop out of the backfill candidate set.
    if to_fetch:
        client.bump_detail_attempts_for_records(to_fetch)

    inserted = stats["inserted_total"]
    updated = stats["updated_total"]
    dupes = stats["duplicates_skipped"]
    real_errors = stats["real_errors"]
    total = stats["total_payload_records"]

    log.info(
        "=== Upsert final: inserted=%d updated=%d duplicates_skipped=%d "
        "real_errors=%d (total=%d) ===",
        inserted, updated, dupes, real_errors, total,
    )
    log.info("=== Run done: id=%s ===", run_id)

    # Failure policy ---------------------------------------------------------
    # 1) Any *real* error (non-409 HTTP, network, transport) -> fail.
    # 2) Duplicate-key skips are non-fatal in normal volumes (official source
    #    publishes a few colliding/null-typed rows every day). But if more
    #    than DUPLICATE_RATIO_THRESHOLD of the payload is skipped as duplicate,
    #    that signals an upstream or schema regression — fail loudly so the
    #    daily issue gets opened and someone investigates.
    DUPLICATE_RATIO_THRESHOLD = 0.10  # 10%

    if real_errors > 0:
        log.error(
            "FAIL: %d real (non-duplicate) upsert errors. See above for HTTP details.",
            real_errors,
        )
        return 1

    if total > 0 and dupes / total > DUPLICATE_RATIO_THRESHOLD:
        log.error(
            "FAIL: duplicate-key skips %d/%d = %.1f%% exceeds threshold %.0f%%. "
            "This usually means a schema/key regression — investigate.",
            dupes, total, 100.0 * dupes / total,
            100.0 * DUPLICATE_RATIO_THRESHOLD,
        )
        return 1

    if dupes > 0:
        log.info(
            "Note: %d duplicate-key rows skipped (%.2f%% of payload) — within "
            "acceptable threshold, treating run as success.",
            dupes, 100.0 * dupes / max(total, 1),
        )

    # Coverage guardrails: warnings always emitted above; escalate to a non-zero
    # exit only when explicitly opted in (FAIL_ON_LOW_COVERAGE=1), so a normal
    # day cannot break production.
    if guardrail_tripped and FAIL_ON_LOW_COVERAGE:
        log.error(
            "FAIL: extraction-coverage guardrail tripped and FAIL_ON_LOW_COVERAGE=1. "
            "See GUARDRAIL warnings above.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
