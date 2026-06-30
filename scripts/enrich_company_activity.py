#!/usr/bin/env python3
"""
enrich_company_activity.py — company "Gegenstand" (business purpose) enrichment.
================================================================================

Fills ``swift_v2.company_activity_sources`` with each insolvent company's official
business purpose (*Gegenstand des Unternehmens*), condensed to 1–2 sentences in
German (``activity_de``) and faithfully translated to Arabic (``activity_ar``).

Pipeline (validated by the spikes — see PR description)
-------------------------------------------------------
PASS 0  (SerpApi — real Google snippets, no scraping, free-tier)
    **SerpApi SEARCH** ``"<name> <city> Handelsregister"`` (one search per
    company) and read only the ``organic_results`` snippets. Feeds the SAME
    identity gate below. Because SerpApi never scrapes, it accepts only when a
    snippet already carries a usable (non-truncated) ``Gegenstand``; otherwise the
    company falls through to PASS 1. SerpApi-derived rows store ``source='aggregator'``.

PASS 1  (Firecrawl — cheap, ~2 credits per query; no-ops if credits are out)
    Firecrawl **SEARCH** a small set of query variants — ``"<name> <city>
    Handelsregister"``, ``"<name> <city> <TYPE>"``, ``"<name> <city>
    Gegenstand"``, ``"<name> <TYPE> <number>"`` — stopping at the first variant
    whose snippets yield an identity match. Reads only the result *snippets*
    (title + description); parses a ``Gegenstand`` text and any registry token
    (HRB/HRA/VR/… + number) and court visible in the snippets.

STRICT MATCH GATE  (mandatory — zero false accepts over 42 spike companies)
    Accept a purpose ONLY if a snippet's registry number matches the company's
    ``registry_number`` (type-aware) AND, *when a court is also visible in that
    snippet*, the court matches ``registry_court``. No match → reject. We never
    display a guessed / foreign same-name company. This gate is unchanged.

PASS 2  (only when identity matched but the snippet purpose is missing/truncated)
    Firecrawl **SCRAPE** the SINGLE identity-matched result whose host is on the
    least-risk allow-list, tried in this order:
    ``unternehmensregister.de`` (official) > ``handelsregister.ai`` >
    ``insolvenz-radar.de`` > ``viaductus.de`` > ``firmeneintrag.creditreform.de``
    > ``northdata.de`` / ``.com``. We re-verify the matching HRB on the scraped
    page before trusting it, never scrape a page that wasn't identity-matched,
    and never scrape ``handelsregister.de``. One page per company, no Stealth.

TRANSLATE
    GPT-4o-mini condenses the German purpose to 1–2 sentences (``activity_de``)
    and produces a faithful Arabic translation (``activity_ar``). Never invents;
    if there is no verified purpose, the company is skipped.

Write
-----
Idempotent UPSERT into ``swift_v2.company_activity_sources`` on
``(entity_id, source)`` where

* ``source`` = the domain class of the page that supplied the purpose:
  ``'unternehmensregister'`` for ``unternehmensregister.de``, else
  ``'aggregator'`` (any other allow-listed host, or a snippet-derived purpose).
* ``confidence`` = ``'high'`` when registry number **and** court both matched,
  else ``'medium'``.
* ``source_ref`` = the matched URL, or ``"<TYPE> <n> / <court>"`` when only the
  snippet identified the company.
* ``matched_hrb`` = the matched registry number.

Writes use ``SUPABASE_SERVICE_ROLE_KEY`` (the table has no role grants).

Environment
-----------
* ``SUPABASE_URL``               — required (real run)
* ``SUPABASE_SERVICE_ROLE_KEY``  — required (real run)
* ``OPENAI_API_KEY``             — required (real run)
* ``SERPAPI_API_KEY``            — required when USE_SERPAPI=1 (PASS 0 provider)
* ``SERPER_API_KEY``             — required when USE_SERPER=1 (PASS 0 provider,
                                   Serper.dev — distinct from SERPAPI_API_KEY)
* ``FIRECRAWL_API_KEY``          — required when USE_FIRECRAWL=1 (PASS 1/2 provider)
                                   At least one provider key must be present.
* ``USE_SERPAPI``                — optional, default ``1`` (PASS 0 SerpApi on)
* ``USE_SERPER``                 — optional, default ``0`` (PASS 0 Serper.dev)
* ``USE_FIRECRAWL``              — optional, default ``1`` (PASS 1/2 Firecrawl on)
* ``SUMMARY_JSON_PATH``          — optional, default ``/tmp/enrich_summary.json``
                                   (health-check summary for the daily email)
* ``MAX_COMPANIES``              — optional int, default 25 (the BATCH SIZE; each
                                   run processes up to this many NOT-yet-enriched
                                   companies, so reruns naturally resume)
* ``CANDIDATE_FETCH_LIMIT``      — optional int, default 1000 (work-list window)
* ``ORDER``                      — optional, default ``entity_id.asc`` (one of
                                   entity_id.asc/desc, announcement_date.asc/desc).
                                   Deterministic scan order for resumable batches.
* ``RETRY_AFTER_DAYS``           — optional int, default 30. A company processed
                                   (accepted OR rejected) within this window is
                                   SKIPPED, so successive batches advance past the
                                   unrecoverable head instead of re-picking it.
                                   ``<= 0`` retries everything. Needs the attempts
                                   ledger table (docs/phase-0053 migration).
* ``ALLOW_STEALTH``              — optional, default ``0`` (OFF). ``1`` enables the
                                   5-credit pass-2 Tier-C Stealth fallback.
* ``REQUEST_PAUSE_SECONDS``      — optional float, default 1.0 (politeness)
* ``TIME_BUDGET_SECONDS``        — optional float, default 3300 (stop before the
                                   60-min job timeout)
* ``DRY_RUN``                    — optional, ``1`` = run the full pipeline but
                                   perform NO DB write (logs what WOULD be written)

Secret-safe: keys are read from the environment only and never printed.
Never writes to ``source_*`` tables; never deletes anything.

When ``DRY_RUN`` is set but the real secrets are absent (e.g. a local checkout),
the script performs a **structural dry run** against a tiny built-in mocked
sample so the gate logic can be exercised offline, and prints a clear notice that
a real dry run must be executed in GitHub Actions where the secrets exist.

Exit codes: 0 ok · 2 config error · 3 fatal transport error.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib import error, parse, request

SCHEMA = "swift_v2"
ACTIVITY_TABLE = "company_activity_sources"
# Backfill progress ledger (see docs/phase-0053-*-migration.sql). One row per
# processed company so selection advances past already-attempted rejects.
ATTEMPTS_TABLE = "company_activity_attempts"
DEFAULT_RETRY_AFTER_DAYS = 30
SOURCE_VIEW = "source_neu_insolvenz_announcements"

# Snippet-only providers (real Google snippets, NO page scraping): they win pass 0
# only with a complete in-snippet Gegenstand, never scrape, and store
# source='aggregator'. Firecrawl is the only scraping provider.
SNIPPET_ONLY_PROVIDERS = frozenset({"serpapi", "serper"})

# PostgREST filters that define the eligible-company universe (used by candidate
# selection AND by the coverage metrics in the health summary — single source).
ELIGIBLE_FILTERS = (
    "subject_type=eq.company"
    "&entity_id=not.is.null"
    "&registry_number=not.is.null"
    "&registry_court=not.is.null"
)

# Exact writable columns of swift_v2.company_activity_sources. id/created_at/
# updated_at are DB-managed and never sent; extracted_at we set explicitly.
# The UPSERT payload keys MUST be a subset of this set (fail-fast guard below).
ACTIVITY_COLUMNS = frozenset({
    "entity_id", "source", "activity_de", "activity_ar",
    "confidence", "source_ref", "matched_hrb", "extracted_at",
})

# A scraped page shorter than this (after strip) is treated as empty / bot-walled.
MIN_SCRAPE_CHARS = 80

# PASS-2 scrape rendering: give slow JS pages time to hydrate before reading, and
# allow a longer per-page timeout. Many "no/low content" pages are just slow JS
# and recover at the normal 1-credit cost with these options.
SCRAPE_WAIT_MS = 3500       # Firecrawl waitFor — wait for client-side render
SCRAPE_TIMEOUT_MS = 60000   # Firecrawl per-page timeout

# Firecrawl credit costs (be frugal — Stealth is 5x a normal scrape).
NORMAL_SCRAPE_CREDITS = 1
STEALTH_SCRAPE_CREDITS = 5

# Map an internal pass-2 attempt status to the logged, human reject reason.
PASS2_REASON = {
    "low_content": "pass2: scrape returned no/low content",
    "mismatch": "pass2: HRB re-verify failed",
    "no_gegenstand": "pass2: no Gegenstand label found on page",
}

# FIX 1 — small, explicit blocklist of "no determinable activity / shell / no
# data" boilerplate. Applied to the TRANSLATED activity (DE or AR) so such rows
# are never stored. Keep this list small and err toward STORING a real activity:
# legitimate purposes like plain "Vermögensverwaltung" (asset management) must
# NOT be blocked — only the explicit shell phrasing ("ohne eigenen
# Geschäftsbetrieb") is. Matched as case-insensitive substrings.
EMPTY_MEANING_DE = (
    "kein operativer geschäftsbetrieb",     # no operative business
    "kein geschäftsbetrieb feststellbar",   # no business determinable
    "keine geschäftstätigkeit",             # no business activity
    "keine angabe",                         # no data (covers "keine Angaben")
    "nicht ermittelbar",                    # not ascertainable
    "nicht feststellbar",                   # not determinable
    "ohne eigenen geschäftsbetrieb",        # holding shell: no own operations
    "ohne operativen geschäftsbetrieb",     # holding shell: no operative business
)
EMPTY_MEANING_AR = (
    "لا يمكن تحديد",      # "cannot determine ..."
    "لا يوجد نشاط",       # "there is no activity"
    "لا توجد معلومات",    # "there is no information"
    "غير محدد",          # "undetermined / unspecified"
)

# Reason recorded when a translated activity is rejected by the FIX-1 filter.
EMPTY_MEANING_REASON = "empty-meaning activity (not stored)"

# unternehmensregister.de is the official, free registry portal. It stays the
# preferred pass-2 scrape target and drives source='unternehmensregister'.
UNTERNEHMENSREGISTER_HOST = "unternehmensregister.de"

# handelsregister.de must NEVER be scraped (ToS / heavy session handling).
HANDELSREGISTER_HOST = "handelsregister.de"

# PASS 2 scrape allow-list, in LEAST-RISK order (index = priority, lower first).
# We scrape the SINGLE identity-matched result with the best rank here; a host
# not in this tuple (or handelsregister.de) is never scraped. unternehmensregister
# stays first/preferred; the rest are aggregators tried only as a fallback.
SCRAPE_DOMAIN_ORDER = (
    "unternehmensregister.de",
    "handelsregister.ai",
    "insolvenz-radar.de",
    "viaductus.de",
    "firmeneintrag.creditreform.de",
    "northdata.de",
    "northdata.com",
)

# Registry tokens as they appear in German registers / snippets.
REG_TOKEN_RE = re.compile(
    r"\b(HRB|HRA|VR|PR|GnR|GsR)\s*\.?\s*([0-9]{1,7})\b", re.IGNORECASE)
# "Amtsgericht <Name>" — used to decide whether a court is *present* in a snippet.
AMTSGERICHT_RE = re.compile(
    r"Amtsgericht\s+([A-Za-zÄÖÜäöüß./\- ]{3,40})", re.IGNORECASE)
# Gegenstand / Unternehmensgegenstand introducers, plus common aggregator
# labels (Tätigkeit / Geschäftszweck / Unternehmenszweck). Longest alternatives
# are listed first so they win over the bare "Gegenstand".
GEGENSTAND_RE = re.compile(
    r"(?:Unternehmensgegenstand|Gegenstand\s+des\s+Unternehmens|"
    r"Gegenstand\s+der\s+Gesellschaft|Geschäftszweck|Unternehmenszweck|"
    r"Tätigkeit|Gegenstand)\s*[:\-–]?\s*(.+)",
    re.IGNORECASE)
ELLIPSIS = ("…", "...", "…")


# --------------------------------------------------------------------------- #
#  Tiny env / http helpers (same shape as scripts/enrich_admins.py)
# --------------------------------------------------------------------------- #
def need(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"[config] missing env {name}", file=sys.stderr)
        sys.exit(2)
    return v


def truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def host_of(url: str) -> str:
    try:
        return (parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def host_matches(host: str, domain: str) -> bool:
    """True when *host* equals *domain* or is a sub-domain of it."""
    return host == domain or host.endswith("." + domain)


def is_unternehmensregister(url: str) -> bool:
    return host_matches(host_of(url), UNTERNEHMENSREGISTER_HOST)


def is_handelsregister_de(url: str) -> bool:
    """handelsregister.de — explicitly never scraped, regardless of allow-list."""
    return host_matches(host_of(url), HANDELSREGISTER_HOST)


def scrape_rank(url: str) -> Optional[int]:
    """Pass-2 scrape priority for *url*.

    Returns the index of the matching host in :data:`SCRAPE_DOMAIN_ORDER`
    (lower = preferred / least-risk), or ``None`` when the host is not on the
    allow-list. ``handelsregister.de`` is always refused (``None``).
    """
    h = host_of(url)
    if not h or host_matches(h, HANDELSREGISTER_HOST):
        return None
    for i, dom in enumerate(SCRAPE_DOMAIN_ORDER):
        if host_matches(h, dom):
            return i
    return None


def source_class(url: str) -> str:
    """Domain class stored in ``source``: official portal vs. aggregator."""
    return "unternehmensregister" if is_unternehmensregister(url) else "aggregator"


def http_json(method: str, url: str, headers: dict[str, str],
              body: Optional[Any] = None, timeout: int = 60) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw else None)
    except error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return e.code, detail
    except Exception as e:  # noqa: BLE001 — surfaced by caller
        return 0, str(e)


def postgrest(method: str, path: str, base: str, key: str,
              body: Optional[Any] = None,
              prefer: Optional[str] = None) -> Any:
    url = f"{base}/rest/v1/{path}"
    headers = {
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json", "Accept": "application/json",
        "Accept-Profile": SCHEMA, "Content-Profile": SCHEMA,
    }
    if prefer:
        headers["Prefer"] = prefer
    status, payload = http_json(method, url, headers, body)
    if status >= 400 or status == 0:
        raise RuntimeError(f"PostgREST {method} {path} -> {status}: {payload}")
    return payload


# --------------------------------------------------------------------------- #
#  Normalisation + the strict identity gate
# --------------------------------------------------------------------------- #
def norm_regnum(s: str) -> str:
    """Digits only, leading zeros stripped (so '0704974' == '704974')."""
    digits = re.sub(r"\D", "", s or "")
    return digits.lstrip("0") or digits


def court_core(name: str) -> str:
    """Reduce a court / city name to a comparable lowercase core token.

    'Münster (Westfalen)' -> 'münster' · 'Freiburg im Breisgau' -> 'freiburg' ·
    'Amtsgericht Kassel'   -> 'kassel'.
    """
    if not name:
        return ""
    n = re.sub(r"amtsgericht", " ", name, flags=re.IGNORECASE)
    first = re.split(r"[\s(/,]", n.strip())[0] if n.strip() else ""
    return re.sub(r"[^a-zäöüß]", "", first.lower())


def detect_court(text: str) -> tuple[bool, set[str]]:
    """Return (court_present, {court_core,...}) for any 'Amtsgericht X' in text."""
    cores: set[str] = set()
    for m in AMTSGERICHT_RE.finditer(text or ""):
        core = court_core(m.group(1))
        if core:
            cores.add(core)
    return (bool(cores), cores)


def identity_match(text: str, reg_type: str, reg_num: str,
                   reg_court: str) -> tuple[bool, bool]:
    """Strict gate over a single snippet / page text.

    Returns ``(identity_ok, court_matched)``.

    * ``identity_ok`` — a registry token in *text* matches the company's
      number (and type, when the company's type is known). If a court is
      *present* in the text it must also match ``reg_court``; an explicit court
      mismatch rejects the snippet outright.
    * ``court_matched`` — True only when a court was present AND matched (drives
      ``confidence='high'``).
    """
    target_num = norm_regnum(reg_num)
    if not target_num:
        return (False, False)
    target_type = (reg_type or "").upper().strip()

    num_ok = False
    for m in REG_TOKEN_RE.finditer(text or ""):
        tok_type = m.group(1).upper()
        tok_num = norm_regnum(m.group(2))
        if tok_num == target_num and (not target_type or tok_type == target_type):
            num_ok = True
            break
    if not num_ok:
        return (False, False)

    court_present, cores = detect_court(text)
    if not court_present:
        return (True, False)            # number-only match -> medium
    target_court = court_core(reg_court)
    court_ok = bool(target_court) and any(
        target_court in c or c in target_court for c in cores)
    if not court_ok:
        return (False, False)           # explicit court mismatch -> reject
    return (True, True)                  # number + court -> high


def verify_hrb_on_page(text: str, reg_num: str) -> str:
    """Tolerant pass-2 re-verification of a scraped page against the company HRB.

    Identity was ALREADY confirmed from the pass-1 snippet, so this is only a
    guard against scraping a clearly *different* company. Comparison is by digits
    only — :func:`norm_regnum` strips spaces/punctuation, leading zeros, the
    ``HRB``/``HRA`` prefix and any trailing court letter — and is type-agnostic.

    Returns one of:
    * ``"match"``    — a registry token on the page matches the company number;
    * ``"mismatch"`` — registry tokens are present but NONE match (different co.);
    * ``"absent"``   — no registry token on the page at all (NOT a mismatch).
    """
    target = norm_regnum(reg_num)
    if not target:
        return "absent"
    nums = [norm_regnum(m.group(2)) for m in REG_TOKEN_RE.finditer(text or "")]
    if not nums:
        return "absent"
    return "match" if target in nums else "mismatch"


def extract_gegenstand(text: str) -> Optional[str]:
    """Pull the Gegenstand clause from a snippet / page; None if not present."""
    if not text:
        return None
    m = GEGENSTAND_RE.search(text)
    if not m:
        return None
    clause = m.group(1).strip()
    # Stop at the next obvious field label that often follows in snippets.
    clause = re.split(
        r"\s+(?:Stammkapital|Vertretung|Geschäftsführer|Gegenstand geändert|"
        r"Prokura|Sitz:|Kapital|HRB |HRA )", clause)[0].strip()
    return clause or None


def is_truncated(purpose: Optional[str]) -> bool:
    """A snippet purpose is unusable when missing, ellipsis-cut, or too short."""
    if not purpose:
        return True
    p = purpose.strip()
    if p.endswith(ELLIPSIS):
        return True
    # Snippets are ~160 chars; a real Gegenstand that doesn't end on a sentence
    # boundary and is short is almost certainly cut off mid-text.
    if len(p) < 60 and not p.endswith((".", "!", ";")):
        return True
    return False


def is_empty_meaning(activity_de: str, activity_ar: str) -> bool:
    """True when the TRANSLATED activity carries no real business activity.

    Catches empty strings and the small DE/AR blocklist of "no determinable
    activity / shell / no data" boilerplate (see EMPTY_MEANING_*), so such rows
    are rejected and never stored. Errs toward storing a genuine activity.
    """
    de = (activity_de or "").strip().lower()
    ar = (activity_ar or "").strip()
    if not de or not ar:
        return True
    if any(p in de for p in EMPTY_MEANING_DE):
        return True
    if any(p in ar for p in EMPTY_MEANING_AR):
        return True
    return False


# --------------------------------------------------------------------------- #
#  Firecrawl (search = pass 1, scrape = pass 2) + OpenAI translate
# --------------------------------------------------------------------------- #
def firecrawl_search(query: str, key: str, limit: int = 5) -> list[dict[str, str]]:
    """Pass 1: snippet-only Google-style search. Returns [{url,title,description}]."""
    status, payload = http_json(
        "POST", "https://api.firecrawl.dev/v1/search",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {"query": query, "limit": limit}, timeout=60)
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"firecrawl search {status}: {str(payload)[:200]}")
    out: list[dict[str, str]] = []
    for r in (payload.get("data") or []):
        out.append({
            "url": r.get("url") or "",
            "title": r.get("title") or "",
            "description": r.get("description") or r.get("snippet") or "",
        })
    return out


def serpapi_search(query: str, key: str, num: int = 10) -> list[dict[str, str]]:
    """PASS 0: real Google snippets via SerpApi (NO page scraping).

    Returns the SAME ``[{url, title, description}]`` shape as
    :func:`firecrawl_search` so the existing snippet parser, identity gate and
    empty-meaning filter are reused unchanged. Invoked with the canonical
    ``"<name> <city> Handelsregister"`` query (one search per company). German
    locale (``hl=de``/``gl=de``) so registry snippets surface.
    """
    url = "https://serpapi.com/search.json?" + parse.urlencode({
        "engine": "google", "q": query, "num": num,
        "hl": "de", "gl": "de", "api_key": key,
    })
    status, payload = http_json("GET", url, {"Accept": "application/json"},
                                timeout=60)
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"serpapi search {status}: {str(payload)[:200]}")
    out: list[dict[str, str]] = []
    for r in (payload.get("organic_results") or []):
        out.append({
            "url": r.get("link") or "",
            "title": r.get("title") or "",
            # 'snippet' is the Google result description; some results also carry
            # a richer 'snippet_highlighted_words' list which we fold in for recall.
            "description": " ".join(filter(None, [
                r.get("snippet") or "",
                " ".join(r.get("snippet_highlighted_words") or []),
            ])).strip(),
        })
    return out


def serper_search(query: str, key: str, num: int = 10) -> list[dict[str, str]]:
    """PASS 0: real Google snippets via Serper.dev (NO page scraping).

    POSTs ``{"q": query, "gl": "de", "hl": "de"}`` to
    ``https://google.serper.dev/search`` with header ``X-API-KEY``. Maps
    ``organic[]`` -> the SAME ``[{url, title, description}]`` shape as the other
    snippet providers, so the existing identity gate / empty-meaning filter /
    translation are reused unchanged. Distinct from SerpApi (SERPAPI_API_KEY).
    """
    status, payload = http_json(
        "POST", "https://google.serper.dev/search",
        {"X-API-KEY": key, "Content-Type": "application/json"},
        {"q": query, "gl": "de", "hl": "de", "num": num}, timeout=60)
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"serper search {status}: {str(payload)[:200]}")
    out: list[dict[str, str]] = []
    for r in (payload.get("organic") or []):
        out.append({
            "url": r.get("link") or "",
            "title": r.get("title") or "",
            "description": r.get("snippet") or "",
        })
    return out


def firecrawl_scrape(url: str, key: str, *, stealth: bool = False) -> str:
    """Pass 2: scrape ONE allow-listed page; returns markdown text.

    Only hosts on :data:`SCRAPE_DOMAIN_ORDER` are scraped; handelsregister.de and
    any other host are refused. One page per call, no retries. Rendering options
    (``waitFor`` + a longer ``timeout``) let slow JS pages hydrate at the normal
    1-credit cost. ``stealth=True`` enables Firecrawl's Stealth proxy — 5 credits,
    used only as a last-resort fallback by the tiered caller.
    """
    if scrape_rank(url) is None:
        raise ValueError(f"refusing to scrape disallowed host: {url}")
    body: dict[str, Any] = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "waitFor": SCRAPE_WAIT_MS,
        "timeout": SCRAPE_TIMEOUT_MS,
    }
    if stealth:
        body["proxy"] = "stealth"   # Firecrawl Enhanced/Stealth proxy (5 credits)
    status, payload = http_json(
        "POST", "https://api.firecrawl.dev/v1/scrape",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        body, timeout=120)
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"firecrawl scrape {status}: {str(payload)[:200]}")
    return (payload.get("data") or {}).get("markdown") or ""


def openai_translate(purpose_de: str, key: str) -> tuple[str, str]:
    """Condense German purpose to 1–2 sentences + faithful Arabic. Never invents."""
    system = (
        "Du bist ein präziser Übersetzer für deutsche Handelsregister-Daten. "
        "Du erhältst den 'Gegenstand des Unternehmens'. Fasse ihn in 1-2 knappen, "
        "sachlichen deutschen Sätzen zusammen (activity_de) und übersetze diese "
        "Zusammenfassung originalgetreu ins Arabische (activity_ar). Erfinde nichts, "
        "füge keine Informationen hinzu. Antworte ausschließlich als JSON mit den "
        "Schlüsseln activity_de und activity_ar."
    )
    status, payload = http_json(
        "POST", "https://api.openai.com/v1/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {
            "model": "gpt-4o-mini",
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": purpose_de},
            ],
        }, timeout=60)
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"openai {status}: {str(payload)[:200]}")
    content = payload["choices"][0]["message"]["content"]
    obj = json.loads(content)
    de = (obj.get("activity_de") or "").strip()
    ar = (obj.get("activity_ar") or "").strip()
    if not de or not ar:
        raise RuntimeError("openai returned empty translation")
    return de, ar


# --------------------------------------------------------------------------- #
#  Per-company decision (pure given injected fns — fully unit-testable)
# --------------------------------------------------------------------------- #
class Decision:
    __slots__ = ("entity_id", "name", "accepted", "reason", "source",
                 "confidence", "source_ref", "matched_hrb", "purpose_de",
                 "purpose_ar", "purpose_raw", "credits", "matched_variant",
                 "pass_used", "court_matched", "scrape_tier", "normal_credits",
                 "stealth_credits", "provider", "serpapi_searches",
                 "serper_searches")

    def __init__(self, entity_id: str, name: str) -> None:
        self.entity_id = entity_id
        self.name = name
        self.accepted = False
        self.reason = ""
        self.source = ""
        self.confidence = ""
        self.source_ref = ""
        self.matched_hrb = ""
        self.purpose_de = ""
        self.purpose_ar = ""
        self.purpose_raw = ""
        self.credits = 0
        self.matched_variant = ""   # which pass-1 query produced the identity match
        self.pass_used = ""         # "1" (snippet) or "2" (scrape)
        self.court_matched = False  # registry court matched on the source page
        self.scrape_tier = ""       # which pass-2 tier succeeded: "A"/"B"/"C"
        self.normal_credits = 0     # 1-credit scrapes spent on this company
        self.stealth_credits = 0    # 5-credit Stealth scrapes spent on this company
        self.provider = ""          # matched provider: "serpapi"/"serper"/"firecrawl"
        self.serpapi_searches = 0   # SerpApi searches spent on this company
        self.serper_searches = 0    # Serper.dev searches spent on this company


def build_query_variants(name: str, city: str, reg_type: str,
                         reg_num: str) -> list[str]:
    """Small set of pass-1 search phrasings, tried in order until one matches.

    Broadens recall over the original single ``"<name> <city> Handelsregister"``
    query (which missed companies whose HRB-bearing snippet only surfaced under a
    different phrasing). De-duplicated; whitespace-collapsed.
    """
    rt = (reg_type or "HRB").strip()
    variants: list[str] = []

    def add(q: str) -> None:
        q = " ".join(q.split())
        if q and q not in variants:
            variants.append(q)

    add(f"{name} {city} Handelsregister")
    add(f"{name} {city} {rt}")
    add(f"{name} {city} Gegenstand")
    add(f"{name} {rt} {reg_num}")
    return variants


def _snippets_have_complete_purpose(
        found: list[tuple[dict[str, str], bool]]) -> bool:
    """True if any matched snippet already yields a non-truncated Gegenstand.

    SerpApi is snippet-only (no scraping), so it may only "win" pass 0 when the
    snippets themselves carry a usable purpose; otherwise we fall through to the
    Firecrawl path (which can recover a truncated purpose by scraping in pass 2).
    """
    for r, _ in found:
        blob = f"{r.get('title', '')} {r.get('description', '')}"
        g = extract_gegenstand(blob)
        if g and not is_truncated(g):
            return True
    return False


def process_company(co: dict[str, Any], *,
                    translate_fn: Callable[[str], tuple[str, str]],
                    serpapi_fn: Optional[Callable[[str], list[dict[str, str]]]] = None,
                    serper_fn: Optional[Callable[[str], list[dict[str, str]]]] = None,
                    search_fn: Optional[Callable[[str], list[dict[str, str]]]] = None,
                    scrape_fn: Optional[Callable[..., str]] = None,
                    allow_stealth: bool = True) -> Decision:
    """Run pass 0/1 snippet gate (+ tiered pass 2) + translate for one company.

    ``co`` needs: entity_id, debtor_name, debtor_city, registry_court,
    registry_type, registry_number. Snippet providers are tried in order —
    ``serpapi_fn`` and ``serper_fn`` (PASS 0, snippet-only, no scraping) then
    ``search_fn`` (Firecrawl PASS 1) — all feeding the SAME strict identity gate.
    ``scrape_fn`` (called as ``scrape_fn(url, stealth=<bool>)``) drives the
    Firecrawl-only pass 2; ``allow_stealth`` gates its 5-credit Tier-C fallback.
    Returns a :class:`Decision` describing the outcome and the UPSERT payload.
    """
    name = (co.get("debtor_name") or "").strip()
    city = (co.get("debtor_city") or "").strip()
    reg_type = (co.get("registry_type") or "").strip()
    reg_num = (co.get("registry_number") or "").strip()
    reg_court = (co.get("registry_court") or "").strip()
    d = Decision(co.get("entity_id") or "", name)

    if not (name and reg_num and reg_court):
        d.reason = "missing name/registry_number/registry_court"
        return d

    # --- PASS 0 (SerpApi / Serper snippets) then PASS 1 (Firecrawl snippets):
    #     all feed the SAME strict identity gate (identity_match is UNCHANGED).
    #     Providers are tried in order; the first to yield an identity match wins.
    #     The snippet-only providers (SNIPPET_ONLY_PROVIDERS) never scrape, so they
    #     win only when their snippets already carry a usable (non-truncated)
    #     Gegenstand; otherwise we fall through (Firecrawl can scrape in pass 2).
    #     Frugal: each snippet provider does ONE search ("<name> <city> Handelsregister").
    variants = build_query_variants(name, city, reg_type, reg_num)
    providers: list[tuple[str, Callable[[str], list[dict[str, str]]], list[str]]] = []
    if serpapi_fn is not None:
        providers.append(("serpapi", serpapi_fn, variants[:1]))
    if serper_fn is not None:
        providers.append(("serper", serper_fn, variants[:1]))
    if search_fn is not None:
        providers.append(("firecrawl", search_fn, variants))

    matched: list[tuple[dict[str, str], bool]] = []  # (result, court_ok)
    for label, sfn, provider_variants in providers:
        for variant in provider_variants:
            results = sfn(variant)
            if label == "firecrawl":
                d.credits += 2           # a Firecrawl search is ~2 credits
            elif label == "serpapi":
                d.serpapi_searches += 1  # SerpApi search (free-tier, no credits)
            else:                        # serper
                d.serper_searches += 1   # Serper.dev search (free-tier, no credits)
            found: list[tuple[dict[str, str], bool]] = []
            for r in results:
                blob = f"{r.get('title','')} {r.get('description','')}"
                ok, court_ok = identity_match(blob, reg_type, reg_num, reg_court)
                if ok:
                    found.append((r, court_ok))
            if not found:
                continue
            if (label in SNIPPET_ONLY_PROVIDERS
                    and not _snippets_have_complete_purpose(found)):
                continue  # snippet-only: no usable purpose -> let next provider try
            d.matched_variant = variant
            d.provider = label
            matched = found
            break
        if matched:
            break

    if not matched:
        d.reason = "no snippet matched registry_number/court (strict gate)"
        return d

    court_matched = any(court_ok for _, court_ok in matched)

    # Best snippet purpose + the URL it came from; plus ALL identity-matched
    # scrapable targets, de-duplicated and ordered least-risk first for the tiered
    # pass-2 escalation below. Prefer a COMPLETE (non-truncated) clause over a
    # longer truncated one — this is what lets a SerpApi snippet accept without
    # scraping when it already carries a full Gegenstand.
    best: Optional[tuple[bool, int, str, str]] = None  # (complete, len, clause, url)
    first_url = ""
    ranked: list[tuple[int, str]] = []
    seen_urls: set[str] = set()
    for r, _ in matched:
        url = r.get("url") or ""
        if not first_url:
            first_url = url
        blob = f"{r.get('title','')} {r.get('description','')}"
        cand = extract_gegenstand(blob)
        if cand:
            key = (not is_truncated(cand), len(cand))
            if best is None or key > (best[0], best[1]):
                best = (key[0], key[1], cand, url)
        rank = scrape_rank(url)
        if rank is not None and url not in seen_urls:
            seen_urls.add(url)
            ranked.append((rank, url))
    ranked.sort(key=lambda t: t[0])
    scrape_targets = [u for _, u in ranked]

    purpose = best[2] if best else None
    purpose_url = best[3] if best else first_url
    d.pass_used = "1"

    # --- PASS 2 (tiered): only when identity matched but the snippet purpose is
    #     missing/truncated. Cheap easy sources first; expensive Stealth last.
    #       TIER A  normal render-scrape of the least-risk matched target (1 cr)
    #       TIER B  normal scrape of the OTHER matched targets, in order (1 cr ea)
    #       TIER C  Stealth retry of the primary target (5 cr) — only when A+B
    #               failed on RENDERING (empty / no label) and allow_stealth.
    #     Identity was already confirmed in pass 1; a page is "usable" iff it
    #     yields a Gegenstand clause and does not show a clearly DIFFERENT HRB.
    pass2_hrb = ""  # "match" | "mismatch" | "absent" on the page that supplied it

    def _attempt(url: str, stealth: bool) -> dict[str, Any]:
        md = scrape_fn(url, stealth=stealth)
        if stealth:
            d.credits += STEALTH_SCRAPE_CREDITS
            d.stealth_credits += STEALTH_SCRAPE_CREDITS
        else:
            d.credits += NORMAL_SCRAPE_CREDITS
            d.normal_credits += NORMAL_SCRAPE_CREDITS
        if not md or len(md.strip()) < MIN_SCRAPE_CHARS:
            return {"status": "low_content"}
        hrb = verify_hrb_on_page(md, reg_num)
        if hrb == "mismatch":
            return {"status": "mismatch"}
        full = extract_gegenstand(md)
        if not full:
            return {"status": "no_gegenstand"}
        court_ok = False
        if hrb == "match":
            # Page re-confirms the HRB -> allow a court upgrade to 'high'.
            _, court_ok = identity_match(md, reg_type, reg_num, reg_court)
        return {"status": "ok", "purpose": full, "hrb": hrb,
                "court_ok": court_ok, "url": url}

    # PASS 2 is Firecrawl-only: snippet-only providers never scrape. Skip it for a
    # snippet-only win (already a complete purpose) or when no scraper is
    # configured (Firecrawl disabled / credits out).
    if (is_truncated(purpose) and d.provider not in SNIPPET_ONLY_PROVIDERS
            and scrape_fn is not None):
        if scrape_targets:
            primary = scrape_targets[0]
            try:
                used: Optional[dict[str, Any]] = None
                tier = ""
                # TIER A — least-risk matched target, rendered normal scrape.
                res = _attempt(primary, stealth=False)
                primary_status = res["status"]
                if res["status"] == "ok":
                    used, tier = res, "A"
                # TIER B — other matched targets, in least-risk order, normal cost.
                if used is None:
                    for alt in scrape_targets[1:]:
                        rb = _attempt(alt, stealth=False)
                        if rb["status"] == "ok":
                            used, tier = rb, "B"
                            break
                # TIER C — Stealth retry of the PRIMARY page, only for rendering
                # failures (a mismatch is the wrong company; Stealth won't help).
                if (used is None and allow_stealth
                        and primary_status in ("low_content", "no_gegenstand")):
                    print(f"[stealth] {name!r}: A+B failed; retrying {primary} "
                          f"with Stealth ({STEALTH_SCRAPE_CREDITS} credits)")
                    rc = _attempt(primary, stealth=True)
                    if rc["status"] == "ok":
                        used, tier = rc, "C"

                if used is not None:
                    purpose = used["purpose"]
                    purpose_url = used["url"]
                    pass2_hrb = used["hrb"]
                    court_matched = court_matched or used["court_ok"]
                    d.pass_used = "2"
                    d.scrape_tier = tier
                else:
                    d.reason = PASS2_REASON.get(
                        primary_status, "pass2: scrape returned no/low content")
                    if allow_stealth and primary_status in (
                            "low_content", "no_gegenstand"):
                        d.reason += " (stealth retried)"
                    d.reason += "; "
            except Exception as e:  # noqa: BLE001 — pass-2 failure is non-fatal
                d.reason = f"pass2 scrape failed: {e}; "
        else:
            d.reason = "identity matched but no scrapable result; "

    d.court_matched = court_matched
    if is_truncated(purpose):
        d.reason += "identity matched but no usable Gegenstand recovered"
        d.matched_hrb = reg_num
        d.confidence = "high" if court_matched else "medium"
        d.source_ref = purpose_url or f"{reg_type} {reg_num} / {reg_court}"
        return d

    # --- translate (never invent) and finalise the accept.
    purpose = purpose.strip()
    de, ar = translate_fn(purpose)

    # FIX 1 — reject "empty-meaning" activities AFTER translation so we never
    # store a no-activity / shell / no-data boilerplate row (no invention, but
    # no useless rows either). matched_hrb/source_ref are kept for the log only.
    if is_empty_meaning(de, ar):
        d.reason = EMPTY_MEANING_REASON
        d.matched_hrb = reg_num
        d.source_ref = purpose_url or f"{reg_type} {reg_num} / {reg_court}"
        return d

    d.accepted = True
    d.reason = "accepted"
    # Snippet-only providers (SerpApi/Serper) are 'aggregator' (per the brief);
    # the Firecrawl path classifies by the page domain that supplied the purpose.
    d.source = ("aggregator" if d.provider in SNIPPET_ONLY_PROVIDERS
                else source_class(purpose_url))
    # high only when number+court matched; a pass-2 page that lacked the HRB was
    # trusted from pass 1 but cannot earn 'high' here -> cap at medium.
    if d.pass_used == "2" and pass2_hrb == "absent":
        d.confidence = "medium"
    else:
        d.confidence = "high" if court_matched else "medium"
    d.source_ref = purpose_url or f"{reg_type} {reg_num} / {reg_court}"
    d.matched_hrb = reg_num
    d.purpose_raw = purpose
    d.purpose_de = de
    d.purpose_ar = ar
    return d


# --------------------------------------------------------------------------- #
#  Candidate selection + upsert
# --------------------------------------------------------------------------- #
# Allowed PostgREST order clauses for ORDER (kept to a safe, deterministic set
# so successive backfill batches process a stable sequence). 'entity_id.asc' is
# the default — fully deterministic and unaffected by new announcements arriving
# between batches.
ALLOWED_ORDERS = (
    "entity_id.asc", "entity_id.desc",
    "announcement_date.desc", "announcement_date.asc",
)
DEFAULT_ORDER = "entity_id.asc"


def parse_ts(s: Optional[str]) -> Optional[datetime]:
    """Parse a PostgREST ISO timestamp to an aware UTC datetime; None on failure."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_attempts(base: str, key: str) -> tuple[dict[str, dict[str, Any]], bool]:
    """Load the attempts ledger -> ({entity_id: {attempts, last_attempt_at}}, ok).

    ``ok`` is False when the ledger table is absent/unreadable (pre-migration);
    callers then treat it as empty AND skip writing attempt rows (so they don't
    crash, but the backfill cannot advance until the 0053 migration is applied).
    """
    try:
        rows = postgrest(
            "GET", f"{ATTEMPTS_TABLE}?select=entity_id,attempts,last_attempt_at",
            base, key) or []
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not read {ATTEMPTS_TABLE} (treating as empty; the "
              f"backfill will NOT advance until the 0053 migration is applied): {e}",
              file=sys.stderr)
        return {}, False
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        eid = r.get("entity_id")
        if eid:
            out[eid] = {"attempts": r.get("attempts") or 0,
                        "last_attempt_at": r.get("last_attempt_at") or ""}
    return out, True


def recently_attempted(attempts_map: dict[str, dict[str, Any]],
                       retry_after_days: int, now: datetime) -> set[str]:
    """entity_ids whose last attempt is within the retry window (skip these).

    ``retry_after_days <= 0`` disables the window (every attempt is retried).
    """
    if retry_after_days <= 0:
        return set()
    cutoff = now - timedelta(days=retry_after_days)
    out: set[str] = set()
    for eid, info in attempts_map.items():
        ts = parse_ts(info.get("last_attempt_at"))
        if ts is not None and ts >= cutoff:
            out.add(eid)
    return out


def fetch_candidates(base: str, key: str, max_companies: int, fetch_limit: int,
                     order: str = DEFAULT_ORDER,
                     exclude: Optional[set[str]] = None,
                     stats: Optional[dict[str, int]] = None) -> list[dict[str, Any]]:
    """Eligible companies that have NO activity row and were NOT recently attempted.

    ADVANCE: a company is selected only when it has no row in
    ``company_activity_sources`` AND its entity_id is not in ``exclude`` (the
    recently-attempted set from the attempts ledger). The view is scanned in
    PAGES of ``fetch_limit`` rows (in deterministic ``order``) until
    ``max_companies`` new candidates are found or the view is exhausted — so the
    batch advances past the unrecoverable head however large it grows, never
    stalling inside a single fetch window.
    """
    if order not in ALLOWED_ORDERS:
        print(f"[warn] ORDER {order!r} not allowed; using {DEFAULT_ORDER!r}",
              file=sys.stderr)
        order = DEFAULT_ORDER
    exclude = exclude or set()
    cols = ("entity_id,debtor_name,debtor_city,registry_court,"
            "registry_type,registry_number,announcement_date")

    # Entities already enriched — graceful if the table is absent (pre-migration).
    already: set[str] = set()
    try:
        seen = postgrest("GET", f"{ACTIVITY_TABLE}?select=entity_id", base, key) or []
        already = {r["entity_id"] for r in seen if r.get("entity_id")}
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not read {ACTIVITY_TABLE} (treating as empty): {e}",
              file=sys.stderr)

    page = max(1, fetch_limit)
    out: list[dict[str, Any]] = []
    chosen: set[str] = set()
    skipped_recent = scanned = offset = 0
    while len(out) < max_companies:
        path = (
            f"{SOURCE_VIEW}?select={cols}&{ELIGIBLE_FILTERS}"
            f"&order={order}"
            f"&offset={offset}&limit={page}"
        )
        rows = postgrest("GET", path, base, key) or []
        if not rows:
            break
        scanned += len(rows)
        for r in rows:
            eid = r.get("entity_id")
            if not eid or eid in chosen or eid in already:
                continue
            if eid in exclude:
                skipped_recent += 1
                continue
            chosen.add(eid)
            out.append(r)
            if len(out) >= max_companies:
                break
        if len(rows) < page:
            break
        offset += page

    if stats is not None:
        stats["already_enriched"] = len(already)
        stats["skipped_recent"] = skipped_recent
        stats["selected"] = len(out)
        stats["scanned"] = scanned
    # ADVANCE visibility: enriched/recently-attempted skipped vs. new selected.
    print(f"[resume] scanned {scanned} announcement rows; {len(already)} already "
          f"enriched, {skipped_recent} skipped (attempted within retry window); "
          f"selected {len(out)} new (batch MAX_COMPANIES={max_companies}, "
          f"order={order})")
    return out


def count_distinct_entity_ids(base: str, key: str, base_path: str,
                              page: int = 1000) -> int:
    """Count DISTINCT entity_ids for an entity_id-selecting PostgREST path.

    Pages through ``base_path`` (which must ``select=entity_id``) and dedups, so a
    view with multiple rows per company yields the true distinct count. Used only
    by the daily health summary (coverage metrics), not the hot path.
    """
    seen: set[str] = set()
    offset = 0
    while True:
        rows = postgrest("GET", f"{base_path}&offset={offset}&limit={page}",
                         base, key) or []
        if not rows:
            break
        for r in rows:
            eid = r.get("entity_id")
            if eid:
                seen.add(eid)
        if len(rows) < page:
            break
        offset += page
    return len(seen)


def build_summary(*, date: str, providers_used: list[str], processed: int,
                  serper_searches: int, accepted: int, written: int,
                  rejected: int, reject_reasons: Counter,
                  high: int, medium: int, total_enriched_now: int,
                  eligible_total: int, ok: bool = True, error: str = "",
                  dry_run: bool = False) -> dict[str, Any]:
    """Assemble the health-check summary dict (also written to JSON for the email).

    Pure (no I/O) so the shape is unit-testable. ``coverage_percent`` =
    total_enriched_now / eligible_total * 100 (0.0 when the denominator is 0).
    """
    coverage = (round(100.0 * total_enriched_now / eligible_total, 1)
                if eligible_total else 0.0)
    return {
        "date": date,
        "providers_used": providers_used,
        "companies_processed": processed,
        "serper_searches_used": serper_searches,
        "accepted": accepted,
        "written": written,
        "rejected": rejected,
        # list of [reason, count], most common first (JSON-friendly)
        "rejected_by_reason": [[r, n] for r, n in reject_reasons.most_common(8)],
        "high": high,
        "medium": medium,
        "total_enriched_now": total_enriched_now,
        "eligible_total": eligible_total,
        "coverage_percent": coverage,
        "ok": ok,
        "error": error,
        "dry_run": dry_run,
    }


def write_summary(path: str, summary: dict[str, Any]) -> None:
    """Write the summary JSON to ``path`` AND echo it to stdout (single line)."""
    blob = json.dumps(summary, ensure_ascii=False)
    print(f"[summary-json] {blob}")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(blob)
    except OSError as e:  # noqa: BLE001 — reporting must not crash the worker
        print(f"[warn] could not write summary JSON to {path}: {e}",
              file=sys.stderr)


def upsert_attempt(base: str, key: str, entity_id: str, attempts: int,
                   reason: str) -> Any:
    """Record one processing attempt (accepted OR rejected). UPSERT on entity_id."""
    row = {
        "entity_id": entity_id,
        "attempts": attempts,
        "last_attempt_at": datetime.now(timezone.utc).isoformat(),
        "last_reason": (reason or "")[:500],
    }
    return postgrest(
        "POST", f"{ATTEMPTS_TABLE}?on_conflict=entity_id", base, key,
        [row], prefer="resolution=merge-duplicates,return=minimal")


def activity_payload(d: Decision) -> dict[str, Any]:
    """Build the UPSERT row and fail fast if it carries any unknown column.

    The row maps the worker's internal fields onto the EXACT live schema of
    ``swift_v2.company_activity_sources``. ``purpose_raw`` is intentionally not a
    column — the raw scraped Gegenstand is folded into ``activity_de`` by the
    translator and never written separately.
    """
    row: dict[str, Any] = {
        "entity_id": d.entity_id,
        "source": d.source,
        "activity_de": d.purpose_de,
        "activity_ar": d.purpose_ar,
        "confidence": d.confidence,
        "source_ref": d.source_ref,
        "matched_hrb": d.matched_hrb,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    unknown = set(row) - ACTIVITY_COLUMNS
    if unknown:
        raise RuntimeError(
            f"refusing to UPSERT unknown column(s) {sorted(unknown)} into "
            f"{SCHEMA}.{ACTIVITY_TABLE}; allowed columns: "
            f"{sorted(ACTIVITY_COLUMNS)}")
    return row


def upsert_activity(base: str, key: str, d: Decision) -> Any:
    return postgrest(
        "POST", f"{ACTIVITY_TABLE}?on_conflict=entity_id,source", base, key,
        [activity_payload(d)], prefer="resolution=merge-duplicates,return=minimal")


# --------------------------------------------------------------------------- #
#  Mocked sample for the offline structural dry run (no network, no DB)
# --------------------------------------------------------------------------- #
# Real candidate rows pulled from swift_v2.source_neu_insolvenz_announcements,
# paired with synthetic Firecrawl-style results that exercise every gate branch:
#   accept-high (number+court, full purpose in snippet),
#   accept-medium (number only, no court in snippet),
#   accept via pass-2 scrape of unternehmensregister.de (snippet truncated),
#   accept via pass-2 scrape of an AGGREGATOR (northdata, snippet truncated),
#   accept via pass-2 with HRB ABSENT on the page + 'Tätigkeit' label (medium),
#   accept via pass-2 TIER C (normal scrape bot-walled -> Stealth recovers it),
#   reject (wrong court — same name, different company),
#   reject (no registry token in any snippet),
#   reject (FIX 1 empty-meaning: holding-shell purpose, not stored).
MOCK_SAMPLE: list[dict[str, Any]] = [
    {
        "company": {
            "entity_id": "0023046a-cdc1-4313-a4b0-43eb9b19d6d0",
            "debtor_name": "Küderle CARE Service UG (haftungsbeschränkt) UG",
            "debtor_city": "Offenburg", "registry_court": "Freiburg im Breisgau",
            "registry_type": "HRB", "registry_number": "704974"},
        "search": [{
            "url": "https://www.unternehmensregister.de/ureg/result.html?id=1",
            "title": "Küderle CARE Service UG (haftungsbeschränkt)",
            "description": ("Amtsgericht Freiburg HRB 704974. Gegenstand des "
                            "Unternehmens: ambulante Pflege- und Betreuungs"
                            "dienstleistungen sowie haushaltsnahe Dienste.")}],
        "scrape": {},
    },
    {
        "company": {
            "entity_id": "006e7810-dbcb-4da5-ab15-ceeb049a410e",
            "debtor_name": "Sprudel IT GmbH für IT Dienstleistungen",
            "debtor_city": "Wendisch Evern", "registry_court": "Lüneburg",
            "registry_type": "HRB", "registry_number": "207355"},
        "search": [{
            "url": "https://northdata.com/Sprudel+IT",
            "title": "Sprudel IT GmbH – HRB 207355",
            "description": ("HRB 207355. Gegenstand: Erbringung von IT-Dienst"
                            "leistungen, Beratung sowie Handel mit Hard- und "
                            "Software.")}],
        "scrape": {},
    },
    {
        "company": {
            "entity_id": "012c9eb4-9b76-4121-b889-f64258430575",
            "debtor_name": "A&M Transfer GmbH", "debtor_city": "Saarbrücken",
            "registry_court": "Saarbrücken", "registry_type": "HRB",
            "registry_number": "106475"},
        "search": [{
            "url": "https://www.unternehmensregister.de/ureg/result.html?id=2",
            "title": "A&M Transfer GmbH, Saarbrücken",
            "description": ("Amtsgericht Saarbrücken HRB 106475. Gegenstand des "
                            "Unternehmens: Güterkraftverkehr, Transport und "
                            "Logistik sowie damit …")}],
        "scrape": {
            "https://www.unternehmensregister.de/ureg/result.html?id=2": (
                "# A&M Transfer GmbH\nAmtsgericht Saarbrücken HRB 106475\n\n"
                "Gegenstand des Unternehmens: Güterkraftverkehr, Transport und "
                "Logistik sowie der Handel mit Fahrzeugen und Ersatzteilen.\n"
                "Stammkapital: 25.000 EUR")},
    },
    {
        # Identity matched on an aggregator, snippet purpose truncated -> pass 2
        # scrapes the SINGLE matched aggregator page and recovers the full text.
        "company": {
            "entity_id": "0190f3b1-2c44-4a8e-9f7c-1b2c3d4e5f60",
            "debtor_name": "Nordlicht Logistik GmbH", "debtor_city": "Bremen",
            "registry_court": "Bremen", "registry_type": "HRB",
            "registry_number": "34567"},
        "search": [{
            "url": "https://www.northdata.com/Nordlicht+Logistik",
            "title": "Nordlicht Logistik GmbH, Bremen – HRB 34567",
            "description": ("Amtsgericht Bremen HRB 34567. Gegenstand des "
                            "Unternehmens: Spedition, Lagerei sowie …")}],
        "scrape": {
            "https://www.northdata.com/Nordlicht+Logistik": (
                "# Nordlicht Logistik GmbH\nAmtsgericht Bremen HRB 34567\n\n"
                "Gegenstand des Unternehmens: Spedition, Lagerei sowie der "
                "nationale und internationale Transport von Gütern aller Art.\n"
                "Stammkapital: 25.000 EUR")},
    },
    {
        # Snippet truncated; the matched aggregator page does NOT restate the HRB
        # but carries a 'Tätigkeit' label. Absence is not a mismatch -> we trust
        # the pass-1 identity and accept the recovered purpose at medium.
        "company": {
            "entity_id": "01a2b3c4-d5e6-47f8-9a0b-1c2d3e4f5a6b",
            "debtor_name": "Seeblick Gastro GmbH", "debtor_city": "Konstanz",
            "registry_court": "Freiburg", "registry_type": "HRB",
            "registry_number": "445566"},
        "search": [{
            "url": "https://www.viaductus.de/seeblick-gastro",
            "title": "Seeblick Gastro GmbH, Konstanz – HRB 445566",
            "description": ("Amtsgericht Freiburg HRB 445566. Gegenstand des "
                            "Unternehmens: Betrieb von …")}],
        "scrape": {
            "https://www.viaductus.de/seeblick-gastro": (
                "# Seeblick Gastro GmbH\nKonstanz\n\n"
                "Tätigkeit: Betrieb von Restaurants und Cafés sowie Catering "
                "und die Ausrichtung von Veranstaltungen.\n"
                "Mitarbeiter: 12")},
    },
    {
        # Single matched result on a HARD aggregator (northdata). The normal
        # render-scrape comes back bot-walled/empty (Tier A fails, no Tier-B
        # alternative) -> Tier C retries the SAME page with Stealth (5 credits),
        # which renders the full Gegenstand. Demonstrates the last-resort path.
        "company": {
            "entity_id": "02b3c4d5-e6f7-48a9-b0c1-2d3e4f5a6b7c",
            "debtor_name": "Hanseatic Stealth Handel GmbH",
            "debtor_city": "Hamburg", "registry_court": "Hamburg",
            "registry_type": "HRB", "registry_number": "667788"},
        "search": [{
            "url": "https://www.northdata.com/Hanseatic+Stealth+Handel",
            "title": "Hanseatic Stealth Handel GmbH, Hamburg – HRB 667788",
            "description": ("Amtsgericht Hamburg HRB 667788. Gegenstand des "
                            "Unternehmens: Im- und Export von …")}],
        "scrape": {
            "https://www.northdata.com/Hanseatic+Stealth+Handel": {
                "normal": "",  # bot wall on the normal 1-credit render scrape
                "stealth": (
                    "# Hanseatic Stealth Handel GmbH\nAmtsgericht Hamburg "
                    "HRB 667788\n\nGegenstand des Unternehmens: Im- und Export "
                    "von Waren aller Art sowie der Groß- und Einzelhandel "
                    "damit.\nStammkapital: 25.000 EUR")},
        },
    },
    {
        "company": {
            "entity_id": "012dac4b-8495-40dc-80d7-0e25ce7ccb89",
            "debtor_name": "ATUGA Bau UG (haftungsbeschränkt)",
            "debtor_city": "Fuldatal", "registry_court": "Kassel",
            "registry_type": "HRB", "registry_number": "18311"},
        "search": [{
            "url": "https://northdata.com/ATUGA",
            "title": "ATUGA Bau UG – Amtsgericht München HRB 18311",
            "description": ("Amtsgericht München HRB 18311. Gegenstand: Hoch- "
                            "und Tiefbau.")}],
        "scrape": {},  # same number, WRONG court (München != Kassel) -> reject
    },
    {
        "company": {
            "entity_id": "014bb689-7162-4316-bfd0-d07de84a1697",
            "debtor_name": "WGT Transporte UG", "debtor_city": "Weingarten",
            "registry_court": "Mannheim", "registry_type": "HRB",
            "registry_number": "790027"},
        "search": [{
            "url": "https://www.firmenwissen.de/wgt",
            "title": "WGT Transporte UG",
            "description": ("Transportunternehmen in Weingarten. Kontakt und "
                            "Öffnungszeiten.")}],
        "scrape": {},  # no registry token anywhere -> reject
    },
    {
        # Identity matches and a full purpose is parsed, but it is a holding-shell
        # boilerplate ("ohne eigenen Geschäftsbetrieb") -> FIX 1 rejects it AFTER
        # translation so no empty-meaning row is stored.
        "company": {
            "entity_id": "03c4d5e6-f7a8-49b0-c1d2-3e4f5a6b7c8d",
            "debtor_name": "Schatten Holding UG", "debtor_city": "Berlin",
            "registry_court": "Charlottenburg", "registry_type": "HRB",
            "registry_number": "224488"},
        "search": [{
            "url": "https://northdata.com/Schatten+Holding",
            "title": "Schatten Holding UG – HRB 224488",
            "description": ("Amtsgericht Charlottenburg HRB 224488. Gegenstand "
                            "des Unternehmens: Verwaltung des eigenen Vermögens "
                            "ohne eigenen Geschäftsbetrieb.")}],
        "scrape": {},
    },
]


def run_mock_dry_run(max_companies: int) -> int:
    print("=" * 74)
    print("STRUCTURAL DRY RUN (offline mocked sample — no secrets in this env)")
    print("Real secrets (FIRECRAWL_API_KEY/OPENAI_API_KEY/SUPABASE_*) are absent,")
    print("so this exercises ONLY the candidate->gate->pass2->payload logic with")
    print("synthetic Firecrawl results. A REAL dry run must run in GitHub Actions.")
    print("=" * 74)

    sample = MOCK_SAMPLE[:max_companies]

    def fake_translate(purpose_de: str) -> tuple[str, str]:
        # Offline stub — a real run calls GPT-4o-mini. Marks output as mocked.
        return (f"[de-mock] {purpose_de[:90]}", "[ar-mock] ترجمة عربية وهمية")

    def fake_scrape(url: str, scrape_map: dict[str, Any], stealth: bool) -> str:
        # A mock value may be a plain string (same for normal/stealth) or a dict
        # {"normal": ..., "stealth": ...} to exercise the Tier-C Stealth path.
        v = scrape_map.get(url, "")
        if isinstance(v, dict):
            return v.get("stealth", "") if stealth else v.get("normal", "")
        return v

    accepted = rejected = empty_meaning = 0
    total_credits = normal_credits = stealth_credits = 0
    tiers = {"A": 0, "B": 0, "C": 0}
    for i, item in enumerate(sample, 1):
        co = item["company"]
        scrape_map = item.get("scrape", {})
        d = process_company(
            co,
            search_fn=lambda q, _s=item["search"]: _s,
            scrape_fn=lambda u, stealth=False, _m=scrape_map: fake_scrape(
                u, _m, stealth),
            translate_fn=fake_translate)
        total_credits += d.credits
        normal_credits += d.normal_credits
        stealth_credits += d.stealth_credits
        if d.scrape_tier in tiers:
            tiers[d.scrape_tier] += 1
        if d.reason == EMPTY_MEANING_REASON:
            empty_meaning += 1
        court_tag = "court+num" if d.court_matched else "num-only"
        if d.accepted:
            accepted += 1
            print(f"[{i}/{len(sample)}] ACCEPT  {d.name!r}")
            print(f"        variant={d.matched_variant!r} match={court_tag} "
                  f"pass={d.pass_used} tier={d.scrape_tier or '-'}")
            print(f"        source={d.source} confidence={d.confidence} "
                  f"matched_hrb={d.matched_hrb}")
            print(f"        credits={d.credits} (norm={d.normal_credits} "
                  f"stealth={d.stealth_credits})")
            print(f"        source_ref={d.source_ref}")
            print(f"        activity_de={d.purpose_de}")
            print(f"        activity_ar={d.purpose_ar}")
            print(f"        WOULD UPSERT into {SCHEMA}.{ACTIVITY_TABLE} "
                  f"on (entity_id, source) [DRY_RUN — no write]")
        else:
            rejected += 1
            print(f"[{i}/{len(sample)}] REJECT  {d.name!r}: {d.reason} "
                  f"[variant={d.matched_variant!r} match={court_tag}]")
    print("-" * 74)
    print(f"[mock-dry-run] companies={len(sample)} accepted={accepted} "
          f"rejected={rejected} empty_meaning_skipped={empty_meaning}")
    print(f"[mock-dry-run] tiers A={tiers['A']} B={tiers['B']} C={tiers['C']} "
          f"| credits total={total_credits} (norm={normal_credits} "
          f"stealth={stealth_credits})")
    print("[mock-dry-run] NOTHING was written to the database.")
    return 0


# --------------------------------------------------------------------------- #
#  Main (real run / real dry run)
# --------------------------------------------------------------------------- #
def main() -> int:
    dry = truthy(os.environ.get("DRY_RUN", ""))
    max_companies = int(os.environ.get("MAX_COMPANIES", "25"))
    fetch_limit = int(os.environ.get("CANDIDATE_FETCH_LIMIT", "1000"))
    pause = float(os.environ.get("REQUEST_PAUSE_SECONDS", "1.0"))
    time_budget = float(os.environ.get("TIME_BUDGET_SECONDS", "3300"))
    # Stealth (Tier C) is OFF by default — 5 credits/page for poor ROI at scale.
    # Set ALLOW_STEALTH=1 to enable it for a targeted run.
    allow_stealth = truthy(os.environ.get("ALLOW_STEALTH", "0"))
    # Deterministic batch ordering for resumable backfills (see fetch_candidates).
    order = os.environ.get("ORDER", DEFAULT_ORDER).strip() or DEFAULT_ORDER
    # Skip companies attempted within this window so the backfill ADVANCES.
    retry_after_days = int(os.environ.get("RETRY_AFTER_DAYS",
                                          str(DEFAULT_RETRY_AFTER_DAYS)))
    summary_path = os.environ.get("SUMMARY_JSON_PATH", "/tmp/enrich_summary.json")
    run_date = datetime.now(timezone.utc).date().isoformat()
    # Snippet providers. SerpApi (PASS 0) is ON by default; Serper (PASS 0) is OFF
    # by default; Firecrawl (PASS 1) is ON by default but no-ops gracefully when its
    # credits are exhausted. Each may be toggled via its flag — at least one must
    # be enabled with a key.
    use_serpapi = truthy(os.environ.get("USE_SERPAPI", "1"))
    use_serper = truthy(os.environ.get("USE_SERPER", "0"))
    use_firecrawl = truthy(os.environ.get("USE_FIRECRAWL", "1"))

    serpapi_key = os.environ.get("SERPAPI_API_KEY", "").strip()
    serper_key = os.environ.get("SERPER_API_KEY", "").strip()
    fire_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    serpapi_on = use_serpapi and bool(serpapi_key)
    serper_on = use_serper and bool(serper_key)
    firecrawl_on = use_firecrawl and bool(fire_key)
    any_provider = serpapi_on or serper_on or firecrawl_on
    providers_used = [p for p, on in (("serpapi", serpapi_on),
                                      ("serper", serper_on),
                                      ("firecrawl", firecrawl_on)) if on]

    # Real run needs Supabase + OpenAI always, and at least one snippet provider.
    have_secrets = (
        all(os.environ.get(k, "").strip() for k in (
            "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "OPENAI_API_KEY"))
        and any_provider)

    # Offline structural dry run when secrets are absent (local checkout).
    if dry and not have_secrets:
        return run_mock_dry_run(max_companies)

    if not any_provider:
        print("[config] no snippet provider available: set SERPAPI_API_KEY "
              "(USE_SERPAPI=1), SERPER_API_KEY (USE_SERPER=1) and/or "
              "FIRECRAWL_API_KEY (USE_FIRECRAWL=1)", file=sys.stderr)
        return 2

    base = need("SUPABASE_URL").rstrip("/")
    skey = need("SUPABASE_SERVICE_ROLE_KEY")
    okey = need("OPENAI_API_KEY")
    fkey = fire_key  # may be "" when Firecrawl is disabled / out of credits
    start = time.monotonic()
    now = datetime.now(timezone.utc)

    # Attempts ledger drives ADVANCE: skip recently-attempted, record every
    # attempt. If the table is absent (pre-0053), proceed read-only-safe but warn
    # that the backfill will re-pick the same head until the migration is applied.
    attempts_map, attempts_ok = load_attempts(base, skey)
    recent = recently_attempted(attempts_map, retry_after_days, now)

    sel_stats: dict[str, int] = {}
    try:
        candidates = fetch_candidates(base, skey, max_companies, fetch_limit,
                                      order, exclude=recent, stats=sel_stats)
    except Exception as e:  # noqa: BLE001
        print(f"[fatal] cannot read candidates: {e}", file=sys.stderr)
        # Emit a failure summary so the daily report still goes out.
        write_summary(summary_path, build_summary(
            date=run_date, providers_used=providers_used, processed=0,
            serper_searches=0, accepted=0, written=0, rejected=0,
            reject_reasons=Counter(), high=0, medium=0, total_enriched_now=0,
            eligible_total=0, ok=False, error=f"cannot read candidates: {e}",
            dry_run=dry))
        return 3

    total = len(candidates)
    if not attempts_ok and not dry:
        print("[warn] attempts ledger unavailable: rejects will NOT be recorded, "
              "so the backfill cannot advance past the head. Apply the 0053 "
              "migration first.", file=sys.stderr)
    print(f"[start] {total} candidate companies (dry_run={dry}, "
          f"max_companies={max_companies}, allow_stealth={allow_stealth}, "
          f"order={order}, retry_after_days={retry_after_days}, "
          f"serpapi={'on' if serpapi_on else 'off'}, "
          f"serper={'on' if serper_on else 'off'}, "
          f"firecrawl={'on' if firecrawl_on else 'off'}, "
          f"attempts_ledger={'on' if attempts_ok else 'ABSENT'})")

    serpapi_fn = (lambda q: serpapi_search(q, serpapi_key)) if serpapi_on else None
    serper_fn = (lambda q: serper_search(q, serper_key)) if serper_on else None
    search_fn = (lambda q: firecrawl_search(q, fkey)) if firecrawl_on else None
    scrape_fn = ((lambda u, stealth=False: firecrawl_scrape(u, fkey, stealth=stealth))
                 if firecrawl_on else None)

    processed = accepted = rejected = written = credits = 0
    normal_credits = stealth_credits = newly_attempted = 0
    serpapi_searches = serper_searches = 0
    provider_accepts: Counter[str] = Counter()
    conf_counts: Counter[str] = Counter()
    tiers = {"A": 0, "B": 0, "C": 0}
    reject_reasons: Counter[str] = Counter()

    def record_attempt(entity_id: Optional[str], reason: str) -> None:
        """Persist one processing attempt so the next batch advances past it."""
        nonlocal newly_attempted
        if not entity_id:
            return
        newly_attempted += 1
        if dry or not attempts_ok:
            return  # dry run writes nothing; pre-migration has no ledger to write
        prev = attempts_map.get(entity_id, {}).get("attempts", 0)
        try:
            upsert_attempt(base, skey, entity_id, prev + 1, reason)
        except Exception as e:  # noqa: BLE001 — ledger write must not kill the run
            print(f"[warn] could not record attempt for {entity_id}: {e}",
                  file=sys.stderr)

    for i, co in enumerate(candidates, 1):
        if time.monotonic() - start > time_budget:
            print(f"[stop] time budget reached after {i - 1} companies")
            break
        processed += 1
        name = (co.get("debtor_name") or "").strip()
        try:
            d = process_company(
                co,
                serpapi_fn=serpapi_fn,
                serper_fn=serper_fn,
                search_fn=search_fn,
                scrape_fn=scrape_fn,
                translate_fn=lambda p: openai_translate(p, okey),
                allow_stealth=allow_stealth)
        except Exception as e:  # noqa: BLE001 — never let one company kill the run
            rejected += 1
            reject_reasons[f"error: {type(e).__name__}"] += 1
            record_attempt(co.get("entity_id"), f"error: {type(e).__name__}")
            print(f"[{i}/{total}] ERROR  {name!r}: {e}")
            time.sleep(pause)
            continue
        credits += d.credits
        normal_credits += d.normal_credits
        stealth_credits += d.stealth_credits
        serpapi_searches += d.serpapi_searches
        serper_searches += d.serper_searches
        if d.scrape_tier in tiers:
            tiers[d.scrape_tier] += 1
        if d.accepted and d.provider:
            provider_accepts[d.provider] += 1
        if d.accepted and d.confidence:
            conf_counts[d.confidence] += 1

        court_tag = "court+num" if d.court_matched else "num-only"
        tier_tag = f"tier={d.scrape_tier}" if d.scrape_tier else "tier=-"
        trace = (f"prov={d.provider or '-'} variant={d.matched_variant!r} "
                 f"match={court_tag} pass={d.pass_used or '-'} {tier_tag} "
                 f"credits~{d.credits} (norm={d.normal_credits} "
                 f"stealth={d.stealth_credits} serp={d.serpapi_searches} "
                 f"serper={d.serper_searches})")
        if not d.accepted:
            rejected += 1
            reject_reasons[(d.reason or "unknown").strip().rstrip(";").strip()] += 1
            record_attempt(co.get("entity_id"), d.reason or "rejected")
            print(f"[{i}/{total}] REJECT {name!r}: {d.reason} [{trace}]")
            time.sleep(pause)
            continue

        accepted += 1
        if dry:
            print(f"[{i}/{total}] ACCEPT {name!r} -> source={d.source} "
                  f"conf={d.confidence} hrb={d.matched_hrb} [dry, no write] [{trace}]")
            print(f"          ref={d.source_ref}")
            print(f"          de={d.purpose_de!r}")
            print(f"          ar={d.purpose_ar!r}")
            record_attempt(co.get("entity_id"), "accepted")
        else:
            try:
                upsert_activity(base, skey, d)
                written += 1
                print(f"[{i}/{total}] WRITE  {name!r} -> source={d.source} "
                      f"conf={d.confidence} hrb={d.matched_hrb} [{trace}]")
                record_attempt(co.get("entity_id"), "accepted")
            except Exception as e:  # noqa: BLE001
                reject_reasons["write failed (db error)"] += 1
                # Do NOT record an attempt on write failure -> retried next run.
                print(f"[{i}/{total}] WRITE FAILED {name!r}: {e}")
        time.sleep(pause)

    empty_meaning = reject_reasons.get(EMPTY_MEANING_REASON, 0)
    print("=" * 74)
    print(f"[summary] selected={total} "
          f"skipped_recently_attempted={sel_stats.get('skipped_recent', 0)} "
          f"newly_attempted={newly_attempted} processed={processed} "
          f"accepted={accepted} written={written} rejected={rejected} "
          f"empty_meaning_skipped={empty_meaning} dry_run={dry}")
    print(f"[summary] provider accepts: serpapi={provider_accepts.get('serpapi', 0)} "
          f"serper={provider_accepts.get('serper', 0)} "
          f"firecrawl={provider_accepts.get('firecrawl', 0)} "
          f"| searches: serpapi={serpapi_searches} serper={serper_searches}")
    print(f"[summary] pass2 tiers A={tiers['A']} B={tiers['B']} C={tiers['C']}")
    print(f"[summary] credits total={credits} "
          f"(firecrawl scrape normal={normal_credits}, stealth={stealth_credits})")
    if reject_reasons:
        print("[summary] rejected-by-reason:")
        for reason, n in reject_reasons.most_common():
            print(f"    {n:5d}  {reason}")
    print("=" * 74)

    # --- Health-check summary JSON (consumed by scripts/send_health_report.py).
    # Coverage metrics are exact distinct-entity counts (daily, off the hot path).
    enriched_path = f"{ACTIVITY_TABLE}?select=entity_id"
    eligible_path = f"{SOURCE_VIEW}?select=entity_id&{ELIGIBLE_FILTERS}"
    try:
        total_enriched_now = count_distinct_entity_ids(base, skey, enriched_path)
        eligible_total = count_distinct_entity_ids(base, skey, eligible_path)
    except Exception as e:  # noqa: BLE001 — metrics must not fail the run
        print(f"[warn] could not compute coverage metrics: {e}", file=sys.stderr)
        total_enriched_now = eligible_total = 0

    write_summary(summary_path, build_summary(
        date=run_date, providers_used=providers_used, processed=processed,
        serper_searches=serper_searches, accepted=accepted, written=written,
        rejected=rejected, reject_reasons=reject_reasons,
        high=conf_counts.get("high", 0), medium=conf_counts.get("medium", 0),
        total_enriched_now=total_enriched_now, eligible_total=eligible_total,
        ok=True, dry_run=dry))
    return 0


if __name__ == "__main__":
    sys.exit(main())
