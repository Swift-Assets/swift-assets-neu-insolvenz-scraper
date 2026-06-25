"""Regression tests for the detail-fetch worker pool.

These guard the production regression from PR #9 where
``fetch_details_with_pool``'s inner ``run_worker`` re-searched the FULL date
range spanning a worker's chunk. A multi-day range can return >1000 rows, which
trips the source's "zu viele Treffer" guard and makes ``search()`` raise — and
because that exception propagated through ``f.result()`` unhandled, it aborted
the entire scheduled run (run id 27847983176).

The fix: always search ONE single day at a time (date_from == date_to), group a
worker's records by announcement_date, and isolate per-date/per-worker failures
so the run completes and persists whatever was fetched.

Run with:  python -m unittest discover -s tests
(stdlib unittest only — no extra test dependency required.)
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper  # noqa: E402
from scraper import InsolvencyRecord, fetch_details_with_pool  # noqa: E402


def _rec(date_str, row_index, case_number) -> InsolvencyRecord:
    return InsolvencyRecord(
        court="AG Berlin",
        case_number=case_number,
        announcement_date=date_str,
        _row_index=row_index,
        _button_id=f"btn_{row_index}",
    )


class _FakeWorker:
    """Stand-in for NeuInsolvenzScraper used by run_worker.

    Records every search() call (as date_from, date_to pairs) on a shared
    class-level list so the test can assert no multi-day range was ever issued.
    ``search_side_effect`` lets a test make a specific date raise.
    """

    search_calls: list = []
    search_side_effect = None  # optional callable(date_from, date_to)

    def __init__(self):
        pass

    def initialize_session(self):
        pass

    def search(self, date_from, date_to):
        type(self).search_calls.append((date_from, date_to))
        if type(self).search_side_effect is not None:
            type(self).search_side_effect(date_from, date_to)
        return []

    def fetch_detail_text(self, row_index, button_id):
        return f"text for row {row_index}"


class _WorkerPoolTestBase(unittest.TestCase):
    def setUp(self):
        _FakeWorker.search_calls = []
        _FakeWorker.search_side_effect = None
        # Patch the scraper class the worker instantiates, the per-fetch sleep
        # (so tests are instant), and the field extractor (so we don't depend on
        # German-text parsing here).
        p1 = mock.patch.object(scraper, "NeuInsolvenzScraper", _FakeWorker)
        p2 = mock.patch.object(scraper, "DETAIL_FETCH_DELAY_SEC", 0)
        p3 = mock.patch.object(
            scraper, "extract_fields_from_text",
            lambda text: {
                "insolvency_administrator": None,
                "opening_date": None,
                "claims_deadline": None,
                "announcement_type_hint": None,
                "announcement_type_raw": None,
                "insolvency_phase": None,
                "is_pre_verteilung": None,
            },
        )
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        self.addCleanup(p3.stop)
        p1.start()
        p2.start()
        p3.start()


class TestSingleDaySearchOnly(_WorkerPoolTestBase):
    def test_one_single_day_search_per_distinct_date(self):
        # A single worker's chunk mixing 3 distinct dates must produce exactly
        # one search per distinct date, each with date_from == date_to.
        listing = [
            _rec("2026-06-12", 0, "A"),
            _rec("2026-06-15", 1, "B"),
            _rec("2026-06-19", 2, "C"),
            _rec("2026-06-12", 3, "D"),  # second record on an existing date
        ]
        fetched = fetch_details_with_pool(listing, workers=1)

        self.assertEqual(fetched, 4)
        # Every search call is a single day (no multi-day range — the bug).
        for date_from, date_to in _FakeWorker.search_calls:
            self.assertEqual(
                date_from, date_to,
                f"multi-day range issued: {date_from}->{date_to}")
        # Exactly one search per distinct date (4 records, 3 dates).
        searched_dates = sorted(df for df, _ in _FakeWorker.search_calls)
        self.assertEqual(
            [d.isoformat() for d in searched_dates],
            ["2026-06-12", "2026-06-15", "2026-06-19"],
        )

    def test_never_searches_the_wide_backlog_range(self):
        # The exact production scenario: a chunk spanning 2026-06-12 .. 2026-06-19
        # must never collapse into the wide range that returned >1000 rows.
        listing = [
            _rec("2026-06-12", 0, "A"),
            _rec("2026-06-19", 1, "B"),
        ]
        fetch_details_with_pool(listing, workers=1)
        self.assertNotIn(
            (scraper.date.fromisoformat("2026-06-12"),
             scraper.date.fromisoformat("2026-06-19")),
            _FakeWorker.search_calls,
        )


class TestFailureIsolation(_WorkerPoolTestBase):
    def test_too_many_results_for_one_date_does_not_abort_run(self):
        # If the re-search for one date raises (e.g. an unexpected "zu viele
        # Treffer"), that date's records are skipped but the OTHER dates still
        # get fetched and counted — and the call returns normally (no crash).
        bad_day = scraper.date.fromisoformat("2026-06-15")

        def boom(date_from, date_to):
            if date_from == bad_day:
                raise RuntimeError(
                    f"Too many results for {date_from}→{date_to} (>1000).")

        _FakeWorker.search_side_effect = boom

        listing = [
            _rec("2026-06-12", 0, "A"),
            _rec("2026-06-15", 1, "B"),   # this date will raise
            _rec("2026-06-19", 2, "C"),
        ]
        # Must NOT raise.
        fetched = fetch_details_with_pool(listing, workers=1)
        # Two good dates fetched; the failing date contributed nothing.
        self.assertEqual(fetched, 2)

    def test_worker_result_exception_is_isolated(self):
        # Even if a worker somehow raises out of run_worker, f.result() must not
        # propagate it and abort the whole run.
        with mock.patch.object(
            scraper, "extract_fields_from_text",
            side_effect=RuntimeError("unexpected worker explosion"),
        ):
            listing = [_rec("2026-06-12", 0, "A")]
            # Must NOT raise; nothing fetched, but the run survives.
            fetched = fetch_details_with_pool(listing, workers=1)
            self.assertEqual(fetched, 0)


class TestMissingDate(_WorkerPoolTestBase):
    def test_none_announcement_date_does_not_build_a_range(self):
        # Records with a missing date must fall back to a single-day search
        # (today), never widen into a range, and never crash.
        listing = [
            _rec(None, 0, "A"),
            _rec("", 1, "B"),
            _rec("2026-06-12", 2, "C"),
        ]
        fetched = fetch_details_with_pool(listing, workers=1)

        self.assertEqual(fetched, 3)
        for date_from, date_to in _FakeWorker.search_calls:
            self.assertEqual(
                date_from, date_to,
                f"multi-day range issued: {date_from}->{date_to}")
        today = scraper.date.today()
        # The None/"" records collapse into a single today() search.
        self.assertIn((today, today), _FakeWorker.search_calls)

    def test_unparseable_date_falls_back_to_single_day(self):
        listing = [_rec("not-a-date", 0, "A")]
        fetched = fetch_details_with_pool(listing, workers=1)
        self.assertEqual(fetched, 1)
        for date_from, date_to in _FakeWorker.search_calls:
            self.assertEqual(date_from, date_to)


if __name__ == "__main__":
    unittest.main()
