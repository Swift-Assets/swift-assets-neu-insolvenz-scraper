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
    MAX_DETAIL_FETCHES          — int (default: 1500)
    DETAIL_WORKERS              — int (default: 4)
    DRY_RUN                     — "1" to skip DB writes
    SKIP_DETAILS                — "1" to only upsert listing-level data
"""

from __future__ import annotations

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
DEFAULT_DETAIL_WORKERS = 4
DEFAULT_MAX_DETAIL_FETCHES = 1500
DETAIL_FETCH_DELAY_SEC = 0.2   # per worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("neu_insolvenz")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

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
    scraped_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # transient — not stored in DB, used internally
    _row_index: int = field(default=-1, repr=False)
    _button_id: str | None = field(default=None, repr=False)

    def unique_key(self) -> tuple:
        """Match the DB unique constraint."""
        return (
            self.court, self.case_number, self.announcement_date,
            self.announcement_type_hint,
            self.registry_court, self.registry_type, self.registry_number,
        )

    def to_db_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop internal fields
        d.pop("_row_index", None)
        d.pop("_button_id", None)
        # Keep nullable fields part of the unique constraint even if None
        keep_null = {
            "announcement_date", "case_number", "court",
            "announcement_type_hint", "registry_court",
            "registry_type", "registry_number",
        }
        return {k: v for k, v in d.items() if v is not None or k in keep_null}


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

TYPE_KEYWORDS = [
    # Order matters: more specific first
    ("Abweisung mangels Masse", "Abweisung mangels Masse"),
    ("Restschuldbefreiung", "Restschuldbefreiung"),
    ("Sicherungsmaßnahme", "Sicherungsmaßnahmen"),
    ("Schlusstermin", "Schlusstermin"),
    ("Schlussverteilung", "Schlussverteilung"),
    ("aufgehoben", "Aufhebung"),
    ("Aufhebung", "Aufhebung"),
    ("eröffnet", "Eröffnung"),
    ("Eröffnung", "Eröffnung"),
    ("eroeffnet", "Eröffnung"),
    ("Eroeffnung", "Eröffnung"),
    ("eingestellt", "Einstellung"),
    ("Einstellung", "Einstellung"),
    ("angeordnet", "Anordnung"),
    ("Anordnung", "Anordnung"),
    ("festgesetzt", "Vergütungsfestsetzung"),
    ("Vergütungsfestsetzung", "Vergütungsfestsetzung"),
    ("Bestellung", "Bestellung"),
    ("Berichtstermin", "Berichtstermin"),
    ("Prüfungstermin", "Prüfungstermin"),
]


def detect_announcement_type(text: str) -> str | None:
    if not text:
        return None
    lower = text.lower()
    for keyword, label in TYPE_KEYWORDS:
        if keyword.lower() in lower:
            return label
    return None


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


def extract_fields_from_text(text: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {
        "insolvency_administrator": None,
        "opening_date": None,
        "claims_deadline": None,
        "announcement_type_hint": detect_announcement_type(text),
    }
    if not text:
        return out

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

    def get_existing_keys(self, date_from: date, date_to: date) -> set[tuple]:
        """Return the set of unique-keys already present in the DB for the date range."""
        endpoint = f"{self.url}/rest/v1/apify_cases"
        params = {
            "select": "court,case_number,announcement_date,announcement_type_hint,registry_court,registry_type,registry_number",
            "announcement_date": f"gte.{date_from.isoformat()}",
        }
        # combine with lte using a second param
        resp = requests.get(
            endpoint, params=params,
            headers={**self.base_headers, "Range-Unit": "items"},
            timeout=60,
        )
        # second filter via additional query
        endpoint2 = f"{self.url}/rest/v1/apify_cases?select={params['select']}&announcement_date=gte.{date_from.isoformat()}&announcement_date=lte.{date_to.isoformat()}"
        resp = requests.get(endpoint2, headers=self.base_headers, timeout=60)
        if resp.status_code >= 400:
            log.warning("Failed to fetch existing keys: %d %s", resp.status_code, resp.text[:200])
            return set()
        rows = resp.json() or []
        return {
            (r.get("court"), r.get("case_number"), r.get("announcement_date"),
             r.get("announcement_type_hint"), r.get("registry_court"),
             r.get("registry_type"), r.get("registry_number"))
            for r in rows
        }

    def upsert_cases(self, records: list[InsolvencyRecord]) -> tuple[int, int]:
        """Calls the public.neu_insolvenz_fill_only_upsert(jsonb) RPC, which:
          - INSERTs new rows (full record).
          - For existing rows: only fills in NULL/empty fields, never overwrites
            enrichment data or non-empty values. Updates updated_at.
        Returns (rows_affected, errors)."""
        if not records:
            return 0, 0

        for r in records:
            r.source_run_id = self.run_id

        endpoint = f"{self.url}/rest/v1/rpc/neu_insolvenz_fill_only_upsert"
        headers = {
            **self.base_headers,
            "Content-Type": "application/json",
        }
        payload_all = [r.to_db_dict() for r in records]

        CHUNK = 100
        ok_count = 0
        err_count = 0
        for i in range(0, len(payload_all), CHUNK):
            chunk = payload_all[i:i + CHUNK]
            try:
                resp = requests.post(
                    endpoint,
                    json={"p_records": chunk},
                    headers=headers,
                    timeout=120,
                )
                if resp.status_code >= 400:
                    log.error("Upsert RPC failed @%d: %d %s", i, resp.status_code, resp.text[:500])
                    err_count += len(chunk)
                else:
                    body = resp.json()
                    if isinstance(body, dict):
                        ok_count += int(body.get("rows_affected", len(chunk)))
                    elif isinstance(body, list):
                        ok_count += len(body)
                    else:
                        ok_count += len(chunk)
            except requests.RequestException as e:
                log.error("Upsert exception @%d: %s", i, e)
                err_count += len(chunk)
        return ok_count, err_count


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def fetch_details_with_pool(
    listing: list[InsolvencyRecord],
    workers: int,
) -> int:
    """For each record in `listing`, fetch its full text and enrich the record.
    Uses `workers` parallel JSF sessions (each session serializes its own
    detail fetches because of the server-side last-clicked-row state)."""
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
        worker = NeuInsolvenzScraper()
        worker.initialize_session()
        # Re-search so this session's tbl_ergebnis matches the row indices
        date_from = date.fromisoformat(work_items[0].announcement_date) if work_items[0].announcement_date else date.today()
        # We need the FULL date range. Pass via attribute set by caller.
        # See main(): we store on the records.
        date_to = date_from
        for it in work_items:
            if it.announcement_date:
                d = date.fromisoformat(it.announcement_date)
                if d < date_from:
                    date_from = d
                if d > date_to:
                    date_to = d
        worker.search(date_from, date_to)

        done = 0
        for rec in work_items:
            try:
                text = worker.fetch_detail_text(rec._row_index, rec._button_id)
                rec.announcement_text = text
                fields = extract_fields_from_text(text)
                rec.insolvency_administrator = fields["insolvency_administrator"]
                rec.opening_date = fields["opening_date"]
                rec.claims_deadline = fields["claims_deadline"]
                rec.announcement_type_hint = fields["announcement_type_hint"]
                done += 1
                with done_lock:
                    nonlocal total_done
                    total_done += 1
                    if total_done % 50 == 0:
                        log.info("Detail progress: %d/%d", total_done, len(listing))
                time.sleep(DETAIL_FETCH_DELAY_SEC)
            except Exception as e:
                log.warning("Detail fetch failed (worker=%d, row=%d, az=%s): %s",
                            worker_id, rec._row_index, rec.case_number, e)
        return done

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_worker, i, chunk) for i, chunk in enumerate(chunks)]
        for f in as_completed(futures):
            f.result()
    return total_done


def main() -> int:
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    dry_run = os.getenv("DRY_RUN") == "1"
    skip_details = os.getenv("SKIP_DETAILS") == "1"
    max_details = int(os.getenv("MAX_DETAIL_FETCHES", str(DEFAULT_MAX_DETAIL_FETCHES)))
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

    # Phase 2: detail fetching — skip records already in DB
    new_records: list[InsolvencyRecord] = listing
    if not dry_run and supabase_url and supabase_key:
        client = SupabaseClient(supabase_url, supabase_key, run_id)
        existing_keys = client.get_existing_keys(date_from, date_to)
        log.info("Existing DB keys in range: %d", len(existing_keys))
        new_records = [r for r in listing if r.unique_key() not in existing_keys]
        log.info("New (not-yet-in-DB) records: %d / %d total listing rows",
                 len(new_records), len(listing))
    else:
        log.info("Dry run — skipping DB key check.")

    if skip_details:
        log.info("SKIP_DETAILS=1 — not fetching announcement_text.")
    else:
        to_fetch = new_records[:max_details]
        if len(new_records) > max_details:
            log.warning("Capped detail fetches at %d (had %d new records).",
                        max_details, len(new_records))
        if to_fetch:
            log.info("Fetching details for %d records with %d workers...",
                     len(to_fetch), workers)
            t0 = time.time()
            fetched = fetch_details_with_pool(to_fetch, workers)
            elapsed = time.time() - t0
            log.info("Detail fetches done: %d/%d in %.1fs (%.2fs avg)",
                     fetched, len(to_fetch), elapsed, elapsed / max(1, fetched))

    # Phase 3: persist
    if dry_run:
        log.info("DRY_RUN=1 — printing sample record and exiting.")
        if listing:
            sample = next((r for r in listing if r.announcement_text), listing[0])
            for k, v in sample.to_db_dict().items():
                preview = (str(v)[:140] + "...") if v and len(str(v)) > 140 else v
                log.info("  %s = %r", k, preview)
        return 0

    client = SupabaseClient(supabase_url, supabase_key, run_id)
    # Upsert all listing rows (cheap insert/no-op for existing ones thanks to UPSERT)
    ok, err = client.upsert_cases(listing)
    log.info("=== Upsert: ok=%d err=%d ===", ok, err)
    log.info("=== Run done: id=%s ===", run_id)
    return 1 if err > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
