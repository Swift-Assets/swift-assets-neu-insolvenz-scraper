"""Regression tests for detail_fetch_attempts persistence.

These guard the production bug where, after a successful run that fetched 1124
texts and attempted many more, ALL rows in public.apify_cases still showed
``detail_fetch_attempts = 0`` and ``last_detail_attempt_at IS NULL``. The
MAX_DETAIL_ATTEMPTS cap was therefore inert: permanently-empty rows re-entered
the backfill set every run and burned the limited detail-fetch budget.

Two root causes:
  1. The old bump only fired for rows whose ``_db_id`` was set (in-window empty
     rows + recovered backlog rows). Brand-new rows — the bulk of an attempt —
     were never counted.
  2. The bump wrote via a partial-row upsert (POST ?on_conflict=id with
     resolution=merge-duplicates) carrying only {id, detail_fetch_attempts,
     last_detail_attempt_at}. PostgREST evaluates that as INSERT-first; the
     partial payload omits NOT-NULL columns so the write silently failed and was
     swallowed as a warning.

The fix resolves the DB id for every attempted row by its stable unique_key()
AFTER the upsert (so even brand-new rows exist), then issues a real per-id PATCH
(no INSERT branch) and reads back to verify the count. These tests prove:
  (a) attempts are incremented +1 with a fresh timestamp for ALL attempted rows
      (mix of new + backfill, with and without a resolved _db_id);
  (b) a row not attempted this run is never touched;
  (c) the write path does NOT depend on _db_id being non-None (a backfill row
      with _db_id=None still gets its attempt recorded via unique_key);
  (d) the write is a real PATCH, never the old partial upsert.

Run with:  python -m unittest discover -s tests
(stdlib unittest only — no extra test dependency required.)
"""

import os
import sys
import unittest
from unittest import mock
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper  # noqa: E402
from scraper import InsolvencyRecord, SupabaseClient  # noqa: E402


def _rec(case_number, date_str="2026-06-19", text=None, db_id=None):
    return InsolvencyRecord(
        court="AG Berlin",
        case_number=case_number,
        announcement_date=date_str,
        announcement_text=text,
        _db_id=db_id,
    )


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload


class _FakeDB:
    """In-memory stand-in for public.apify_cases behind PostgREST.

    Models only what the attempt-bump path touches: rows keyed by id, each with
    a stable-key tuple, an announcement_date, detail_fetch_attempts and
    last_detail_attempt_at. Records every PATCH and POST so tests can assert the
    write path used and that exactly the attempted rows were updated.
    """

    def __init__(self):
        # id -> row dict
        self.rows = {}
        self.patch_calls = []   # list of (id, body)
        self.post_calls = []    # list of (url, payload)  — the OLD broken path
        self.get_calls = []

    def add_row(self, id_, rec, attempts=0, last_at=None):
        self.rows[id_] = {
            "id": id_,
            "court": rec.court,
            "case_number": rec.case_number,
            "announcement_date": rec.announcement_date,
            "registry_court": rec.registry_court,
            "registry_type": rec.registry_type,
            "registry_number": rec.registry_number,
            "announcement_text": rec.announcement_text,
            "detail_fetch_attempts": attempts,
            "last_detail_attempt_at": last_at,
        }

    # ---- HTTP verb handlers --------------------------------------------------
    def get(self, url, headers=None, timeout=None):
        self.get_calls.append(url)
        q = parse_qs(urlparse(url).query)
        select = q.get("select", [""])[0]
        rows = list(self.rows.values())

        # Filter: announcement_date=eq.<d>
        if "announcement_date" in q:
            val = q["announcement_date"][0]
            if val.startswith("eq."):
                d = val[3:]
                rows = [r for r in rows if r["announcement_date"] == d]

        # Filter: id=in.(a,b,c)
        if "id" in q:
            val = q["id"][0]
            if val.startswith("in.("):
                ids = val[len("in.("):-1].split(",")
                ids = [i for i in ids if i]
                rows = [r for r in rows if r["id"] in ids]

        # Filter: last_detail_attempt_at=eq.<ts>  (the verify read-back)
        if "last_detail_attempt_at" in q:
            val = q["last_detail_attempt_at"][0]
            if val.startswith("eq."):
                ts = val[3:]
                rows = [r for r in rows if r["last_detail_attempt_at"] == ts]

        # Project the selected columns (best-effort; full row is fine for tests)
        cols = [c for c in select.split(",") if c]
        out = [{c: r.get(c) for c in cols} for r in rows] if cols else rows
        return _FakeResp(200, out)

    def patch(self, url, json=None, headers=None, timeout=None):
        q = parse_qs(urlparse(url).query)
        id_filter = q.get("id", [""])[0]
        assert id_filter.startswith("eq."), f"PATCH must filter id=eq.<id>: {url}"
        id_ = id_filter[3:]
        self.patch_calls.append((id_, dict(json)))
        if id_ in self.rows:
            self.rows[id_].update(json)
            return _FakeResp(204)
        return _FakeResp(404, text="no row")

    def post(self, url, json=None, headers=None, timeout=None):
        # The OLD broken path posted to ?on_conflict=id with merge-duplicates.
        # If anything calls it, record it so the test can fail loudly.
        self.post_calls.append((url, json))
        return _FakeResp(201)


class _AttemptsTestBase(unittest.TestCase):
    def setUp(self):
        self.db = _FakeDB()
        self.client = SupabaseClient("https://example.supabase.co", "svc-key", "run-1")
        p_get = mock.patch.object(scraper.requests, "get", self.db.get)
        p_patch = mock.patch.object(scraper.requests, "patch", self.db.patch)
        p_post = mock.patch.object(scraper.requests, "post", self.db.post)
        self.addCleanup(p_get.stop)
        self.addCleanup(p_patch.stop)
        self.addCleanup(p_post.stop)
        p_get.start()
        p_patch.start()
        p_post.start()


class TestBumpPersistsForAllAttempted(_AttemptsTestBase):
    def test_all_attempted_rows_incremented_with_fresh_timestamp(self):
        # Mix of: a brand-new row (no _db_id, fetched text this run), an in-window
        # backfill row (had _db_id), and a backlog row (had _db_id). ALL exist in
        # the DB now (post-upsert) and must be incremented +1.
        new_row = _rec("NEW-1", text="fetched text")          # _db_id None
        backfill_a = _rec("BF-A", db_id="id-bf-a")            # was empty
        backfill_b = _rec("BF-B", db_id="id-bf-b")            # was empty
        self.db.add_row("id-new-1", new_row, attempts=0)
        self.db.add_row("id-bf-a", backfill_a, attempts=1)
        self.db.add_row("id-bf-b", backfill_b, attempts=2)

        updated = self.client.bump_detail_attempts_for_records(
            [new_row, backfill_a, backfill_b])

        self.assertEqual(updated, 3, "all 3 attempted rows must be verified updated")
        # Each row incremented by exactly 1.
        self.assertEqual(self.db.rows["id-new-1"]["detail_fetch_attempts"], 1)
        self.assertEqual(self.db.rows["id-bf-a"]["detail_fetch_attempts"], 2)
        self.assertEqual(self.db.rows["id-bf-b"]["detail_fetch_attempts"], 3)
        # Each row got a fresh timestamp.
        for rid in ("id-new-1", "id-bf-a", "id-bf-b"):
            self.assertIsNotNone(self.db.rows[rid]["last_detail_attempt_at"])

    def test_does_not_depend_on_db_id(self):
        # Core of the bug: a backfill row with _db_id=None still gets its attempt
        # recorded, because resolution is by unique_key() (re-queried from DB),
        # not by the transient _db_id.
        backfill_no_id = _rec("BF-NOID", text=None, db_id=None)
        self.db.add_row("id-bf-noid", backfill_no_id, attempts=1)

        updated = self.client.bump_detail_attempts_for_records([backfill_no_id])

        self.assertEqual(updated, 1)
        self.assertEqual(self.db.rows["id-bf-noid"]["detail_fetch_attempts"], 2)
        self.assertIsNotNone(self.db.rows["id-bf-noid"]["last_detail_attempt_at"])

    def test_uses_real_patch_not_partial_upsert(self):
        rec = _rec("X-1", db_id="id-x")
        self.db.add_row("id-x", rec, attempts=0)

        self.client.bump_detail_attempts_for_records([rec])

        # The fix must use a real PATCH, never the old broken POST-upsert.
        self.assertTrue(self.db.patch_calls, "expected a real PATCH write")
        self.assertEqual(
            self.db.post_calls, [],
            "must NOT use the old partial POST ?on_conflict=id upsert path")
        # And the PATCH body carries the increment + timestamp, nothing partial.
        _id, body = self.db.patch_calls[0]
        self.assertEqual(body["detail_fetch_attempts"], 1)
        self.assertIn("last_detail_attempt_at", body)


class TestBumpLeavesUnattemptedRowsAlone(_AttemptsTestBase):
    def test_row_not_attempted_is_untouched(self):
        attempted = _rec("ATT-1", db_id="id-att")
        other = _rec("OTHER-1", db_id="id-other")
        self.db.add_row("id-att", attempted, attempts=0, last_at=None)
        self.db.add_row("id-other", other, attempts=0, last_at=None)

        self.client.bump_detail_attempts_for_records([attempted])

        # Attempted row bumped...
        self.assertEqual(self.db.rows["id-att"]["detail_fetch_attempts"], 1)
        self.assertIsNotNone(self.db.rows["id-att"]["last_detail_attempt_at"])
        # ...the other row is completely untouched (no double-count, no stamp).
        self.assertEqual(self.db.rows["id-other"]["detail_fetch_attempts"], 0)
        self.assertIsNone(self.db.rows["id-other"]["last_detail_attempt_at"])
        # And only the attempted id was ever PATCHed.
        patched_ids = {pid for pid, _ in self.db.patch_calls}
        self.assertEqual(patched_ids, {"id-att"})

    def test_no_double_count_when_same_key_listed_twice(self):
        # Defensive: if the same row appears twice in to_fetch (e.g. in-window +
        # backlog collision), it must only be incremented once.
        rec = _rec("DUP-1", db_id="id-dup")
        self.db.add_row("id-dup", rec, attempts=0)

        self.client.bump_detail_attempts_for_records([rec, rec])

        self.assertEqual(self.db.rows["id-dup"]["detail_fetch_attempts"], 1)
        self.assertEqual(len([p for p, _ in self.db.patch_calls if p == "id-dup"]), 1)


class TestResolveIdsByKey(_AttemptsTestBase):
    def test_resolution_is_null_safe_and_by_normalized_key(self):
        # A record whose key columns are None/empty must still match the DB row
        # via _norm_key_val normalization (mirrors unique_key()).
        rec = InsolvencyRecord(
            court="AG Berlin", case_number="N-1", announcement_date="2026-06-19",
            registry_court=None, registry_type="", registry_number=None,
        )
        self.db.add_row("id-norm", rec, attempts=0)

        key_to_id = self.client.resolve_ids_by_key([rec])

        self.assertEqual(key_to_id.get(rec.unique_key()), "id-norm")

    def test_unresolvable_row_reports_zero_and_warns(self):
        # A row with no matching DB row resolves to nothing; bump returns 0 so a
        # silent failure is visible (caller logs a WARNING).
        ghost = _rec("GHOST", date_str="2099-01-01")
        updated = self.client.bump_detail_attempts_for_records([ghost])
        self.assertEqual(updated, 0)


if __name__ == "__main__":
    unittest.main()
