#!/usr/bin/env python3
"""
enrich_admins.py — one-time / on-demand Insolvenzverwalter e-mail enrichment.
============================================================================

Reads the work-list ``swift_v2.v_admin_enrichment_worklist`` (unique insolvency
administrators that appear in acquirable / pre-Verteilung cases and have NO
e-mail anywhere yet), finds each one's professional Kanzlei e-mail via a single
SerpAPI Google search + a polite Impressum/Kontakt fetch, writes verified
addresses back into ``swift_v2.insolvency_administrators`` via the
``set_admin_email_by_norm`` RPC, and finally calls
``fn_propagate_admin_contact`` so every case of that administrator inherits the
contact.

Design goals
------------
* **Bounded & one-time.** Processes at most ``MAX_ADMINS`` rows per run (the
  work-list shrinks as e-mails are found), so cost is a one-off, not a
  continuous pipeline.
* **Conservative.** Only writes an e-mail found on the administrator's OWN
  firm domain (never a directory/aggregator like cylex, anwaltauskunft, …),
  and prefers an address whose local-part contains the administrator surname.
  Never invents/guesses an address.
* **Secret-safe.** Keys are read from the environment only; never printed.

Environment
-----------
* ``SUPABASE_URL``               — required
* ``SUPABASE_SERVICE_ROLE_KEY``  — required
* ``SERPAPI_KEY``                — required (same key the old edge fn used)
* ``MAX_ADMINS``                 — optional int, default 300
* ``REQUEST_PAUSE_SECONDS``      — optional float, default 1.0 (politeness)
* ``DRY_RUN``                    — optional, ``1`` = search but do not write

Exit codes: 0 ok · 2 config error · 3 fatal transport error.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Optional
from urllib import error, parse, request

SCHEMA = "swift_v2"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Aggregators / directories whose e-mails are NOT the administrator's own.
DIRECTORY_DOMAINS = {
    "cylex.de", "web2.cylex.de", "cylex-branchenbuch-rostock.de", "justico.de",
    "anwaltauskunft.de", "insolvenz-portal.de", "app.insolvenz-portal.de",
    "11880.com", "dastelefonbuch.de", "personensuche.dastelefonbuch.de",
    "yasni.de", "linkedin.com", "de.linkedin.com", "facebook.com", "xing.com",
    "kununu.com", "anwalt.de", "anwalt-suchservice.de", "rechtecheck.de",
    "northdata.com", "north-data.com", "openpr.de", "google.com",
    "wikipedia.org", "versteigerungskalender.de", "insolvenz-radar.de",
    "diebewertung.de", "der-indat.de", "rws-verlag.de", "lehmanns.de",
    "verband-deutscher-anwaelte.de", "taxlegis.de", "foruminsolvenz.net",
    "oeffnungszeitenbuch.de", "pointoo.de", "gmail.com", "t-online.de",
    "web.de", "gmx.de", "gmx.net", "hotmail.com", "outlook.com",
}
# Local-parts that are noise even on the right domain.
BAD_LOCALPARTS = {"datenschutz", "noreply", "no-reply", "webmaster", "abuse",
                  "newsletter", "presse", "marketing"}
IMPRESSUM_PATHS = ["", "/impressum", "/kontakt", "/impressum.html",
                   "/kontakt.html", "/impressum/", "/kontakt/"]


def need(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"[config] missing env {name}", file=sys.stderr)
        sys.exit(2)
    return v


def http_get(url: str, timeout: int = 20) -> tuple[int, str]:
    req = request.Request(url, headers={
        "User-Agent": "SwiftAssets-AdminEnrich/1.0 (+https://swift-assets.de)",
        "Accept": "text/html,application/json",
    })
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, resp.read().decode(charset, errors="replace")
    except error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001 — best-effort fetch
        return 0, ""


def postgrest(method: str, path: str, base: str, key: str,
              body: Optional[Any] = None) -> Any:
    url = f"{base}/rest/v1/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method, headers={
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json", "Accept": "application/json",
        "Accept-Profile": SCHEMA, "Content-Profile": SCHEMA,
    })
    with request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def host_of(url: str) -> str:
    try:
        return parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""


def registrable(host: str) -> str:
    """Crude eTLD+1 (good enough for .de / .com firm domains)."""
    host = host.lower().lstrip("www.")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_directory(host: str) -> bool:
    h = host.lower()
    return any(h == d or h.endswith("." + d) or registrable(h) == d
               for d in DIRECTORY_DOMAINS)


def serpapi_links(query: str, key: str) -> list[str]:
    params = parse.urlencode({
        "engine": "google", "q": query, "api_key": key,
        "num": "6", "hl": "de", "gl": "de", "google_domain": "google.de",
    })
    status, raw = http_get(f"https://serpapi.com/search.json?{params}")
    if status != 200 or not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    links: list[str] = []
    kg = data.get("knowledge_graph") or {}
    if kg.get("website"):
        links.append(kg["website"])
    for r in (data.get("organic_results") or []):
        if r.get("link"):
            links.append(r["link"])
    # Keep only plausible firm sites, in result order, de-duped by domain.
    seen: set[str] = set()
    firm_links: list[str] = []
    for u in links:
        h = host_of(u)
        if not h or is_directory(h):
            continue
        reg = registrable(h)
        if reg in seen:
            continue
        seen.add(reg)
        firm_links.append(f"{parse.urlsplit(u).scheme or 'https'}://{h}")
    return firm_links[:3]


def pick_email(emails: list[str], firm_host: str, surname: str) -> Optional[str]:
    firm_reg = registrable(firm_host)
    cands = []
    for e in emails:
        e = e.strip().strip(".,;:)»\"'")
        local, _, dom = e.lower().partition("@")
        if not dom or local in BAD_LOCALPARTS:
            continue
        if is_directory(dom):
            continue
        if registrable(dom) != firm_reg:   # must be on the firm's own domain
            continue
        cands.append(e.lower())
    if not cands:
        return None
    # Prefer surname in local-part, then kanzlei/info, else first.
    sn = surname.lower()
    for e in cands:
        if sn and sn in e.split("@")[0]:
            return e
    for pref in ("kanzlei", "info", "mail", "kontakt", "inso"):
        for e in cands:
            if e.split("@")[0].startswith(pref):
                return e
    return cands[0]


def surname_of(name: str) -> str:
    cleaned = re.sub(r"\b(Rechtsanw[aä]lt(in)?|Prof\.?|Dr\.?|LL\.M\.?|"
                     r"Fachanwalt|Dipl\.?-?\w*|Notar(in)?)\b", " ", name, flags=re.I)
    toks = [t for t in re.split(r"[\s,]+", cleaned) if len(t) > 1 and any(c.isalpha() for c in t)]
    return toks[-1] if toks else ""


def find_email(name: str, city: str, key: str, pause: float) -> Optional[tuple[str, str]]:
    surname = surname_of(name)
    query = f'"{name}" Insolvenzverwalter {city} Kanzlei E-Mail Impressum'.strip()
    for firm in serpapi_links(query, key):
        firm_host = host_of(firm)
        for path in IMPRESSUM_PATHS:
            status, html = http_get(firm + path)
            if status != 200 or not html:
                continue
            emails = EMAIL_RE.findall(html)
            chosen = pick_email(emails, firm_host, surname)
            if chosen:
                return chosen, firm_host
            time.sleep(pause)
        time.sleep(pause)
    return None


def main() -> int:
    base = need("SUPABASE_URL").rstrip("/")
    skey = need("SUPABASE_SERVICE_ROLE_KEY")
    serp = need("SERPAPI_KEY")
    max_admins = int(os.environ.get("MAX_ADMINS", "300"))
    pause = float(os.environ.get("REQUEST_PAUSE_SECONDS", "1.0"))
    dry = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")

    try:
        worklist = postgrest(
            "GET",
            f"v_admin_enrichment_worklist?select=norm_name,name,city,address&limit={max_admins}",
            base, skey)
    except Exception as e:  # noqa: BLE001
        print(f"[fatal] cannot read work-list: {e}", file=sys.stderr)
        return 3

    total = len(worklist or [])
    print(f"[start] {total} administrators to enrich (dry_run={dry})")
    found = written = 0
    for i, row in enumerate(worklist or [], 1):
        name = (row.get("name") or "").strip()
        city = (row.get("city") or "").strip()
        if not city and row.get("address"):
            m = re.search(r"\b\d{5}\s+([A-Za-zÄÖÜäöüß .\-]+)", row["address"])
            city = m.group(1).strip() if m else ""
        if not name:
            continue
        try:
            hit = find_email(name, city, serp, pause)
        except Exception as e:  # noqa: BLE001 — never let one admin kill the run
            print(f"[{i}/{total}] {name!r}: error {e}")
            continue
        if not hit:
            print(f"[{i}/{total}] {name!r}: no firm e-mail found")
            continue
        email, src = hit
        found += 1
        if dry:
            print(f"[{i}/{total}] {name!r} -> {email}  (src {src})  [dry]")
            continue
        try:
            n = postgrest("POST", "rpc/set_admin_email_by_norm", base, skey, {
                "p_norm_name": row["norm_name"], "p_email": email, "p_firm": None})
            written += 1
            print(f"[{i}/{total}] {name!r} -> {email}  (src {src}, rows {n})")
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{total}] {name!r} -> {email}  WRITE FAILED: {e}")

    if not dry:
        try:
            res = postgrest("POST", "rpc/fn_propagate_admin_contact", base, skey, {})
            print(f"[propagate] {res}")
        except Exception as e:  # noqa: BLE001
            print(f"[propagate] failed: {e}", file=sys.stderr)

    print(f"[done] found={found} written={written} of {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
