#!/usr/bin/env python3
"""
run_swift_v2_backfill.py
========================

Drains the Swift V2 entity backfill backlog by calling the Supabase RPC
``swift_v2.backfill_neu_insolvenz_direct_entities(p_limit)`` in a **bounded,
batched loop** after a successful scraper run.

This is *Option 1* (temporary): the scraper keeps writing to
``public.apify_cases``; this script then copies / links the newly arrived
``source_actor = 'neu_insolvenz_direct'`` rows into the new Swift V2 entity
architecture (``swift_v2.source_neu_insolvenz_announcements``,
``swift_v2.portal_entities``, ``swift_v2.entity_source_links``).

Why bounded batches
-------------------
The RPC used to be called with ``p_limit = NULL`` (process the entire backlog
in a single transaction). Once volume grew (~7.6k rows) that single unbounded
call exceeded PostgREST's ~8s statement timeout (Postgres ``57014: canceling
statement due to statement timeout``).

The DB function is now **forward-progressing**: it only selects ``apify_cases``
rows that still need work (not yet ingested, OR ``entity_id`` is null, OR
missing their ``entity_source_links`` row), ordered ``created_at ASC`` and
bounded by ``p_limit``. So repeated bounded calls drain the backlog and, when
nothing needs work, the function returns ``source_rows_changed = 0``. This
makes a bounded batched loop both safe (each call stays well under the timeout)
and guaranteed to terminate.

Environment
-----------
- ``SUPABASE_URL``                — required, e.g. ``https://<project>.supabase.co``
- ``SUPABASE_SERVICE_ROLE_KEY``   — required, service role JWT
- ``BACKFILL_BATCH_SIZE``         — optional, positive int (default 1000).
                                    Passed as ``p_limit`` on each RPC call.
- ``BACKFILL_MAX_BATCHES``        — optional, positive int (default 100).
                                    Hard safety cap on the number of iterations
                                    so the script ALWAYS terminates.

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
- ``0``  → backlog drained, or the safety cap was hit cleanly (HTTP 2xx throughout)
- ``2``  → configuration error (missing env vars / bad URL / bad batch config)
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
TIMEOUT_SECONDS = 600  # each call is now bounded; allow ample headroom

DEFAULT_BATCH_SIZE = 1000
DEFAULT_MAX_BATCHES = 100

RESULT_FIELDS = (
    "source_rows_changed",
    "source_rows_total",
    "entities_total",
    "source_rows_linked",
    "links_total",
)


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


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        _fail(2, f"{name} must be a positive integer, got {raw!r}")
    if value <= 0:
        _fail(2, f"{name} must be a positive integer, got {value}")
    return value


def _call_rpc(endpoint: str, service_key: str, p_limit: int) -> tuple[int, str]:
    """Make a single bounded RPC call. On HTTP/transport error this exits the
    process (preserving exit codes 3/4) so a real DB problem fails the run."""
    body = json.dumps({"p_limit": p_limit}).encode("utf-8")

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
        _fail(4, f"RPC returned HTTP {e.code}: {_redact(err_body) or e.reason}")
        raise  # unreachable; for type checkers
    except error.URLError as e:
        _fail(3, f"HTTP transport error: {_redact(str(e.reason))}")
        raise
    except Exception as e:  # noqa: BLE001
        _fail(3, f"Unexpected error: {_redact(type(e).__name__)}")
        raise

    if not (200 <= status < 300):
        _fail(4, f"RPC returned non-2xx HTTP {status}: {_redact(raw[:500])}")

    return status, raw


def _parse_row(status: int, raw: str) -> dict[str, Any]:
    """Parse the single-row RPC result. The RPC returns TABLE(...) → PostgREST
    wraps it as a list with one dict."""
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[run_swift_v2_backfill] HTTP {status}, non-JSON response "
              f"(first 200 chars): {raw[:200]!r}")
        return {}

    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        return parsed[0]
    if isinstance(parsed, dict):
        return parsed
    return {}


def _write_step_summary(batches_run: int, capped: bool, row: dict[str, Any]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n### Swift V2 backfill result\n\n")
            f.write(f"`{SCHEMA}.{RPC_NAME}(p_limit)` drained over "
                    f"{batches_run} batch(es)")
            if capped:
                f.write(" — **safety cap reached, backlog may remain**")
            f.write("\n\n")
            f.write("| Metric | Value |\n|---|---|\n")
            for field in RESULT_FIELDS:
                f.write(f"| `{field}` | {row.get(field, 'n/a')} |\n")
    except OSError:
        # Summary file is optional; never let it break the job
        pass


def main() -> None:
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not supabase_url:
        _fail(2, "SUPABASE_URL is not set")
    if not service_key:
        _fail(2, "SUPABASE_SERVICE_ROLE_KEY is not set")
    if not supabase_url.startswith("https://"):
        _fail(2, "SUPABASE_URL must start with https://")

    batch_size = _positive_int_env("BACKFILL_BATCH_SIZE", DEFAULT_BATCH_SIZE)
    max_batches = _positive_int_env("BACKFILL_MAX_BATCHES", DEFAULT_MAX_BATCHES)

    endpoint = f"{supabase_url}/rest/v1/rpc/{RPC_NAME}"
    print(f"[run_swift_v2_backfill] Calling RPC: {SCHEMA}.{RPC_NAME}(p_limit)")
    print(f"[run_swift_v2_backfill] Endpoint:   {endpoint}")
    print(f"[run_swift_v2_backfill] Profile:    {SCHEMA}")
    print(f"[run_swift_v2_backfill] Batch size: {batch_size}")
    print(f"[run_swift_v2_backfill] Max batches: {max_batches}")

    last_row: dict[str, Any] = {}
    batches_run = 0
    capped = False

    while True:
        if batches_run >= max_batches:
            capped = True
            print(f"[run_swift_v2_backfill] WARNING: reached "
                  f"BACKFILL_MAX_BATCHES={max_batches} safety cap. Backlog may "
                  f"remain; the next scheduled run will continue draining it. "
                  f"Stopping without error.")
            break

        batch_no = batches_run + 1
        status, raw = _call_rpc(endpoint, service_key, batch_size)
        row = _parse_row(status, raw)
        last_row = row
        batches_run = batch_no

        changed = row.get("source_rows_changed")
        print(f"[run_swift_v2_backfill] Batch {batch_no} (HTTP {status}):")
        for field in RESULT_FIELDS:
            value = row[field] if field in row else "<not returned>"
            print(f"  - {field:25s}: {value}")

        # Forward-progressing function: it only selects rows that NEED work.
        # 0 changed  → backlog fully drained.
        # < batch    → fewer than a full batch remained → backlog now drained.
        # == batch   → a full batch processed → more may remain → continue.
        if not isinstance(changed, int):
            # Defensive: if the count is missing/non-int we cannot reason about
            # progress, so stop rather than risk looping.
            print("[run_swift_v2_backfill] source_rows_changed missing or "
                  "non-integer; stopping after this batch.")
            break
        if changed == 0:
            print("[run_swift_v2_backfill] Backlog fully drained "
                  "(source_rows_changed == 0).")
            break
        if changed < batch_size:
            print(f"[run_swift_v2_backfill] Partial batch "
                  f"({changed} < {batch_size}); backlog drained.")
            break
        # changed == batch_size → continue to next iteration.

    print("[run_swift_v2_backfill] ✅ Backfill RPC loop complete "
          f"({batches_run} batch(es) run).")
    print("[run_swift_v2_backfill] Final result summary:")
    for field in RESULT_FIELDS:
        value = last_row[field] if field in last_row else "<not returned>"
        print(f"  - {field:25s}: {value}")

    _write_step_summary(batches_run, capped, last_row)

    sys.exit(0)


if __name__ == "__main__":
    main()
