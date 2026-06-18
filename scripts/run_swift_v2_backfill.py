#!/usr/bin/env python3
"""
run_swift_v2_backfill.py
========================

Calls the Supabase RPC ``swift_v2.backfill_neu_insolvenz_direct_entities(null)``
after a successful scraper run.

This is *Option 1* (temporary): the scraper keeps writing to
``public.apify_cases``; this script then copies / links the newly arrived
``source_actor = 'neu_insolvenz_direct'`` rows into the new Swift V2 entity
architecture (``swift_v2.source_neu_insolvenz_announcements``,
``swift_v2.portal_entities``, ``swift_v2.entity_source_links``).

Environment
-----------
- ``SUPABASE_URL``                — required, e.g. ``https://<project>.supabase.co``
- ``SUPABASE_SERVICE_ROLE_KEY``   — required, service role JWT

Secrets handling
----------------
The service-role key is read from the process environment only. It is **never**
printed, logged, written to disk, or sent back into Git. Only HTTP status,
RPC payload result fields and (truncated) error bodies are printed.

PostgREST schema selection
--------------------------
The RPC lives in the ``swift_v2`` schema (not ``public``). By default
Supabase PostgREST only serves ``public`` (plus any schemas configured under
``db-schemas`` in API settings). To target ``swift_v2`` we send::

    Accept-Profile:  swift_v2
    Content-Profile: swift_v2

per PostgREST's profile-negotiation rules. If the project's API config does
not include ``swift_v2`` under ``db-schemas``, the call will return HTTP 404
``schema "swift_v2" not exposed`` — in that case add ``swift_v2`` to:

    Project Settings → API → Exposed schemas

(see docs/option-1-backfill-automation-report.md for the exact procedure).

Exit code
---------
- ``0``  → RPC succeeded (HTTP 2xx)
- ``2``  → configuration error (missing env vars / bad URL)
- ``3``  → HTTP request transport error (timeouts, DNS, etc.)
- ``4``  → RPC returned non-2xx HTTP status
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib import error, request

RPC_NAME = "backfill_neu_insolvenz_direct_entities"
SCHEMA = "swift_v2"
TIMEOUT_SECONDS = 600  # backfill of ~4k rows is fast, but allow headroom


def _fail(code: int, msg: str) -> None:
    print(f"[run_swift_v2_backfill] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _redact(s: str) -> str:
    """Belt-and-braces: ensure the service role key never leaks via prints
    even if some error path stringified the headers."""
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if key and key in s:
        s = s.replace(key, "***REDACTED***")
    return s


def main() -> None:
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not supabase_url:
        _fail(2, "SUPABASE_URL is not set")
    if not service_key:
        _fail(2, "SUPABASE_SERVICE_ROLE_KEY is not set")
    if not supabase_url.startswith("https://"):
        _fail(2, "SUPABASE_URL must start with https://")

    endpoint = f"{supabase_url}/rest/v1/rpc/{RPC_NAME}"
    print(f"[run_swift_v2_backfill] Calling RPC: {SCHEMA}.{RPC_NAME}(null)")
    print(f"[run_swift_v2_backfill] Endpoint:   {endpoint}")
    print(f"[run_swift_v2_backfill] Profile:    {SCHEMA}")

    body = json.dumps({"p_limit": None}).encode("utf-8")

    req = request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Profile": SCHEMA,
            "Content-Profile": SCHEMA,
            "Prefer": "return=representation",
        },
    )

    try:
        with request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:1000]
        except Exception:  # noqa: BLE001
            pass
        _fail(
            4,
            f"RPC returned HTTP {e.code}: {_redact(err_body) or e.reason}",
        )
        return  # for type checkers
    except error.URLError as e:
        _fail(3, f"HTTP transport error: {_redact(str(e.reason))}")
        return
    except Exception as e:  # noqa: BLE001
        _fail(3, f"Unexpected error: {_redact(type(e).__name__)}")
        return

    if not (200 <= status < 300):
        _fail(4, f"RPC returned non-2xx HTTP {status}: {_redact(raw[:500])}")

    # ---- Parse and summarize ----
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[run_swift_v2_backfill] HTTP {status}, non-JSON response "
              f"(first 200 chars): {raw[:200]!r}")
        print("[run_swift_v2_backfill] Treating as success (HTTP 2xx).")
        sys.exit(0)

    # The RPC returns TABLE(...) → PostgREST wraps it as a list of dicts.
    row: dict[str, Any] = {}
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        row = parsed[0]
    elif isinstance(parsed, dict):
        row = parsed

    print("[run_swift_v2_backfill] ✅ Backfill RPC succeeded")
    print("[run_swift_v2_backfill] Result summary:")
    for field in (
        "source_rows_changed",
        "source_rows_total",
        "entities_total",
        "source_rows_linked",
        "links_total",
    ):
        if field in row:
            print(f"  - {field:25s}: {row[field]}")
        else:
            print(f"  - {field:25s}: <not returned>")

    # Append to GitHub Actions step summary if available
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write("\n### Swift V2 backfill result\n\n")
                f.write(f"`{SCHEMA}.{RPC_NAME}(null)` → HTTP {status}\n\n")
                f.write("| Metric | Value |\n|---|---|\n")
                for field in (
                    "source_rows_changed",
                    "source_rows_total",
                    "entities_total",
                    "source_rows_linked",
                    "links_total",
                ):
                    f.write(f"| `{field}` | {row.get(field, 'n/a')} |\n")
        except OSError:
            # Summary file is optional; never let it break the job
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
