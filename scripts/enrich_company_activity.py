#!/usr/bin/env python3
"""
enrich_company_activity.py — company "Gegenstand" (business purpose) enrichment.
================================================================================

Fills ``swift_v2.company_activity_sources`` with each insolvent company's official
business purpose (*Gegenstand des Unternehmens*), condensed to 1–2 sentences in
German (``activity_de``) and faithfully translated to Arabic (``activity_ar``).

Pipeline (validated by the spikes — see PR description)
-------------------------------------------------------
PASS 1  (cheap, ~2 credits per query)
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
* ``FIRECRAWL_API_KEY``          — required (real run)
* ``OPENAI_API_KEY``             — required (real run)
* ``MAX_COMPANIES``              — optional int, default 25 (small by design)
* ``CANDIDATE_FETCH_LIMIT``      — optional int, default 1000 (work-list window)
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
from typing import Any, Callable, Optional
from urllib import error, parse, request

SCHEMA = "swift_v2"
ACTIVITY_TABLE = "company_activity_sources"
SOURCE_VIEW = "source_neu_insolvenz_announcements"

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
# Gegenstand / Unternehmensgegenstand introducers.
GEGENSTAND_RE = re.compile(
    r"(?:Unternehmensgegenstand|Gegenstand\s+des\s+Unternehmens|"
    r"Gegenstand\s+der\s+Gesellschaft|Gegenstand)\s*[:\-–]?\s*(.+)",
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


def firecrawl_scrape(url: str, key: str) -> str:
    """Pass 2: scrape ONE allow-listed page; returns markdown text.

    Only hosts on :data:`SCRAPE_DOMAIN_ORDER` are scraped; handelsregister.de
    and any other host are refused. One page per call (no Stealth, no retries).
    """
    if scrape_rank(url) is None:
        raise ValueError(f"refusing to scrape disallowed host: {url}")
    status, payload = http_json(
        "POST", "https://api.firecrawl.dev/v1/scrape",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {"url": url, "formats": ["markdown"], "onlyMainContent": True},
        timeout=90)
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
                 "pass_used", "court_matched")

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


def process_company(co: dict[str, Any], *,
                    search_fn: Callable[[str], list[dict[str, str]]],
                    scrape_fn: Callable[[str], str],
                    translate_fn: Callable[[str], tuple[str, str]]) -> Decision:
    """Run pass 1 + gate (+ pass 2) + translate for one company.

    ``co`` needs: entity_id, debtor_name, debtor_city, registry_court,
    registry_type, registry_number. Returns a :class:`Decision` describing the
    accept/reject outcome and (when accepted) the payload to UPSERT.
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

    # --- PASS 1: try query variants, stop at the first that yields an identity
    #     match. The strict gate (identity_match) is UNCHANGED.
    matched: list[tuple[dict[str, str], bool]] = []  # (result, court_ok)
    for variant in build_query_variants(name, city, reg_type, reg_num):
        results = search_fn(variant)
        d.credits += 2  # a Firecrawl search is ~2 credits
        found: list[tuple[dict[str, str], bool]] = []
        for r in results:
            blob = f"{r.get('title','')} {r.get('description','')}"
            ok, court_ok = identity_match(blob, reg_type, reg_num, reg_court)
            if ok:
                found.append((r, court_ok))
        if found:
            d.matched_variant = variant
            matched = found
            break

    if not matched:
        d.reason = "no snippet matched registry_number/court (strict gate)"
        return d

    court_matched = any(court_ok for _, court_ok in matched)

    # Best snippet purpose (longest) + the URL it came from; plus the single
    # least-risk scrape target among the identity-matched results.
    snippet_purpose: Optional[str] = None
    purpose_url = ""
    first_url = ""
    scrape_url = ""
    best_rank: Optional[int] = None
    for r, _ in matched:
        url = r.get("url") or ""
        if not first_url:
            first_url = url
        blob = f"{r.get('title','')} {r.get('description','')}"
        cand = extract_gegenstand(blob)
        if cand and (snippet_purpose is None or len(cand) > len(snippet_purpose)):
            snippet_purpose = cand
            purpose_url = url
        rank = scrape_rank(url)
        if rank is not None and (best_rank is None or rank < best_rank):
            best_rank = rank
            scrape_url = url

    purpose = snippet_purpose
    if not purpose_url:
        purpose_url = first_url
    d.pass_used = "1"

    # --- PASS 2: only when identity matched but the snippet purpose is
    #     missing/truncated. Scrape the SINGLE least-risk matched URL, then
    #     re-verify identity on that page before trusting it.
    if is_truncated(purpose):
        if scrape_url:
            try:
                md = scrape_fn(scrape_url)
                d.credits += 1  # a Firecrawl scrape is ~1 credit
                full = extract_gegenstand(md)
                ok2, court_ok2 = identity_match(md, reg_type, reg_num, reg_court)
                if full and ok2:
                    purpose = full
                    purpose_url = scrape_url
                    court_matched = court_matched or court_ok2
                    d.pass_used = "2"
                else:
                    d.reason = ("pass2 scrape did not re-verify identity / "
                                "no Gegenstand on page; ")
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
    d.accepted = True
    d.reason = "accepted"
    d.source = source_class(purpose_url)
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
def fetch_candidates(base: str, key: str, max_companies: int,
                     fetch_limit: int) -> list[dict[str, Any]]:
    """Distinct company entities with a registry, not yet in the activity table."""
    cols = ("entity_id,debtor_name,debtor_city,registry_court,"
            "registry_type,registry_number,announcement_date")
    path = (
        f"{SOURCE_VIEW}?select={cols}"
        "&subject_type=eq.company"
        "&entity_id=not.is.null"
        "&registry_number=not.is.null"
        "&registry_court=not.is.null"
        "&order=announcement_date.desc"
        f"&limit={fetch_limit}"
    )
    rows = postgrest("GET", path, base, key) or []

    # Entities already enriched — graceful if the table is absent (pre-migration).
    already: set[str] = set()
    try:
        seen = postgrest("GET", f"{ACTIVITY_TABLE}?select=entity_id", base, key) or []
        already = {r["entity_id"] for r in seen if r.get("entity_id")}
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not read {ACTIVITY_TABLE} (treating as empty): {e}",
              file=sys.stderr)

    out: list[dict[str, Any]] = []
    chosen: set[str] = set()
    for r in rows:
        eid = r.get("entity_id")
        if not eid or eid in chosen or eid in already:
            continue
        chosen.add(eid)
        out.append(r)
        if len(out) >= max_companies:
            break
    return out


def upsert_activity(base: str, key: str, d: Decision) -> Any:
    body = [{
        "entity_id": d.entity_id,
        "source": d.source,
        "activity_de": d.purpose_de,
        "activity_ar": d.purpose_ar,
        "purpose_raw": d.purpose_raw,
        "confidence": d.confidence,
        "source_ref": d.source_ref,
        "matched_hrb": d.matched_hrb,
    }]
    return postgrest(
        "POST", f"{ACTIVITY_TABLE}?on_conflict=entity_id,source", base, key,
        body, prefer="resolution=merge-duplicates,return=minimal")


# --------------------------------------------------------------------------- #
#  Mocked sample for the offline structural dry run (no network, no DB)
# --------------------------------------------------------------------------- #
# Real candidate rows pulled from swift_v2.source_neu_insolvenz_announcements,
# paired with synthetic Firecrawl-style results that exercise every gate branch:
#   accept-high (number+court, full purpose in snippet),
#   accept-medium (number only, no court in snippet),
#   accept via pass-2 scrape of unternehmensregister.de (snippet truncated),
#   accept via pass-2 scrape of an AGGREGATOR (northdata, snippet truncated),
#   reject (wrong court — same name, different company),
#   reject (no registry token in any snippet).
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

    accepted = rejected = 0
    total_credits = 0
    for i, item in enumerate(sample, 1):
        co = item["company"]
        scrape_map = item.get("scrape", {})
        d = process_company(
            co,
            search_fn=lambda q, _s=item["search"]: _s,
            scrape_fn=lambda u, _m=scrape_map: _m.get(u, ""),
            translate_fn=fake_translate)
        total_credits += d.credits
        court_tag = "court+num" if d.court_matched else "num-only"
        if d.accepted:
            accepted += 1
            print(f"[{i}/{len(sample)}] ACCEPT  {d.name!r}")
            print(f"        variant={d.matched_variant!r} match={court_tag} "
                  f"pass={d.pass_used}")
            print(f"        source={d.source} confidence={d.confidence} "
                  f"matched_hrb={d.matched_hrb}")
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
          f"rejected={rejected} est_credits={total_credits}")
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

    have_secrets = all(os.environ.get(k, "").strip() for k in (
        "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY",
        "FIRECRAWL_API_KEY", "OPENAI_API_KEY"))

    # Offline structural dry run when secrets are absent (local checkout).
    if dry and not have_secrets:
        return run_mock_dry_run(max_companies)

    base = need("SUPABASE_URL").rstrip("/")
    skey = need("SUPABASE_SERVICE_ROLE_KEY")
    fkey = need("FIRECRAWL_API_KEY")
    okey = need("OPENAI_API_KEY")
    start = time.monotonic()

    try:
        candidates = fetch_candidates(base, skey, max_companies, fetch_limit)
    except Exception as e:  # noqa: BLE001
        print(f"[fatal] cannot read candidates: {e}", file=sys.stderr)
        return 3

    total = len(candidates)
    print(f"[start] {total} candidate companies (dry_run={dry}, "
          f"max_companies={max_companies})")

    accepted = rejected = written = credits = 0
    for i, co in enumerate(candidates, 1):
        if time.monotonic() - start > time_budget:
            print(f"[stop] time budget reached after {i - 1} companies")
            break
        name = (co.get("debtor_name") or "").strip()
        try:
            d = process_company(
                co,
                search_fn=lambda q: firecrawl_search(q, fkey),
                scrape_fn=lambda u: firecrawl_scrape(u, fkey),
                translate_fn=lambda p: openai_translate(p, okey))
        except Exception as e:  # noqa: BLE001 — never let one company kill the run
            rejected += 1
            print(f"[{i}/{total}] ERROR  {name!r}: {e}")
            time.sleep(pause)
            continue
        credits += d.credits

        court_tag = "court+num" if d.court_matched else "num-only"
        trace = (f"variant={d.matched_variant!r} match={court_tag} "
                 f"pass={d.pass_used or '-'} credits~{d.credits}")
        if not d.accepted:
            rejected += 1
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
        else:
            try:
                upsert_activity(base, skey, d)
                written += 1
                print(f"[{i}/{total}] WRITE  {name!r} -> source={d.source} "
                      f"conf={d.confidence} hrb={d.matched_hrb} [{trace}]")
            except Exception as e:  # noqa: BLE001
                print(f"[{i}/{total}] WRITE FAILED {name!r}: {e}")
        time.sleep(pause)

    print(f"[done] companies={total} accepted={accepted} rejected={rejected} "
          f"written={written} est_credits={credits} dry_run={dry}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
