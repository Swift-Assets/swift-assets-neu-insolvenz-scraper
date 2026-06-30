"""Unit tests for the company-activity (Gegenstand) enrichment gate.

These guard the STRICT identity match gate that gave zero false accepts over the
42 spike companies: a purpose is accepted ONLY when a snippet's registry number
matches (type-aware) and — when a court is also visible in that snippet — the
court matches too. An explicit court mismatch (same name, different company) must
reject. We also cover the pass-1 query-variant fallback, the pass-2 path
(truncated snippet -> scrape the SINGLE identity-matched result), the least-risk
scrape-domain ordering, the re-verification of the scraped page, and the refusal
to scrape handelsregister.de / any non-allow-listed host.

Run with:  python -m unittest discover -s tests
(stdlib unittest only — no extra test dependency required.)
"""

import json
import os
import re
import sys
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import enrich_company_activity as eca  # noqa: E402


class TestNormAndCourt(unittest.TestCase):
    def test_norm_regnum_strips_leading_zeros_and_nondigits(self):
        self.assertEqual(eca.norm_regnum("0704974"), "704974")
        self.assertEqual(eca.norm_regnum("HRB 704974 B"), "704974")
        self.assertEqual(eca.norm_regnum(""), "")

    def test_court_core(self):
        self.assertEqual(eca.court_core("Freiburg im Breisgau"), "freiburg")
        self.assertEqual(eca.court_core("Münster (Westfalen)"), "münster")
        self.assertEqual(eca.court_core("Amtsgericht Kassel"), "kassel")


class TestIdentityGate(unittest.TestCase):
    def test_number_and_court_match_is_high(self):
        text = ("Amtsgericht Freiburg HRB 704974. Gegenstand des Unternehmens: "
                "ambulante Pflege.")
        ok, court = eca.identity_match(text, "HRB", "704974", "Freiburg im Breisgau")
        self.assertTrue(ok)
        self.assertTrue(court)

    def test_number_only_no_court_is_medium(self):
        text = "HRB 207355. Gegenstand: IT-Dienstleistungen."
        ok, court = eca.identity_match(text, "HRB", "207355", "Lüneburg")
        self.assertTrue(ok)
        self.assertFalse(court)

    def test_wrong_court_same_number_is_rejected(self):
        text = "Amtsgericht München HRB 18311. Gegenstand: Hoch- und Tiefbau."
        ok, court = eca.identity_match(text, "HRB", "18311", "Kassel")
        self.assertFalse(ok)
        self.assertFalse(court)

    def test_wrong_number_is_rejected(self):
        text = "Amtsgericht Kassel HRB 99999."
        ok, _ = eca.identity_match(text, "HRB", "18311", "Kassel")
        self.assertFalse(ok)

    def test_wrong_type_same_number_is_rejected(self):
        text = "Amtsgericht Kassel HRA 18311."
        ok, _ = eca.identity_match(text, "HRB", "18311", "Kassel")
        self.assertFalse(ok)

    def test_no_registry_token_is_rejected(self):
        text = "Transportunternehmen in Weingarten. Kontakt und Öffnungszeiten."
        ok, _ = eca.identity_match(text, "HRB", "790027", "Mannheim")
        self.assertFalse(ok)


class TestGegenstand(unittest.TestCase):
    def test_extract_basic(self):
        text = ("HRB 1. Gegenstand des Unternehmens: Handel mit Waren aller Art. "
                "Stammkapital: 25.000 EUR")
        self.assertEqual(eca.extract_gegenstand(text),
                         "Handel mit Waren aller Art.")

    def test_extract_none_when_absent(self):
        self.assertIsNone(eca.extract_gegenstand("HRB 1. Stammkapital 25.000 EUR"))

    def test_extract_aggregator_labels(self):
        # Broadened labels used on aggregator pages.
        self.assertEqual(
            eca.extract_gegenstand("Tätigkeit: Betrieb von Cafés."),
            "Betrieb von Cafés.")
        self.assertEqual(
            eca.extract_gegenstand("Geschäftszweck: Handel mit Waren."),
            "Handel mit Waren.")
        self.assertEqual(
            eca.extract_gegenstand("Unternehmenszweck: Beratung von Firmen."),
            "Beratung von Firmen.")

    def test_is_truncated(self):
        self.assertTrue(eca.is_truncated(None))
        self.assertTrue(eca.is_truncated("Güterkraftverkehr sowie damit …"))
        self.assertTrue(eca.is_truncated("Handel"))  # short, no terminal punct
        self.assertFalse(eca.is_truncated("Handel mit Hard- und Software."))


class TestScrapeHostGuard(unittest.TestCase):
    def test_is_unternehmensregister(self):
        self.assertTrue(eca.is_unternehmensregister(
            "https://www.unternehmensregister.de/ureg/result.html?id=1"))
        self.assertFalse(eca.is_unternehmensregister("https://northdata.com/x"))
        self.assertFalse(eca.is_unternehmensregister("https://handelsregister.de/x"))

    def test_scrape_rank_least_risk_order(self):
        # unternehmensregister.de is preferred (rank 0); aggregators follow in
        # the documented least-risk order.
        self.assertEqual(
            eca.scrape_rank("https://www.unternehmensregister.de/x"), 0)
        self.assertEqual(eca.scrape_rank("https://handelsregister.ai/x"), 1)
        self.assertEqual(eca.scrape_rank("https://insolvenz-radar.de/x"), 2)
        self.assertEqual(eca.scrape_rank("https://viaductus.de/x"), 3)
        self.assertEqual(
            eca.scrape_rank("https://firmeneintrag.creditreform.de/x"), 4)
        self.assertEqual(eca.scrape_rank("https://www.northdata.de/x"), 5)
        self.assertEqual(eca.scrape_rank("https://northdata.com/x"), 6)
        # unternehmensregister ranks strictly ahead of every aggregator.
        self.assertLess(
            eca.scrape_rank("https://unternehmensregister.de/x"),
            eca.scrape_rank("https://northdata.com/x"))

    def test_scrape_rank_refuses_handelsregister_de_and_unknown(self):
        self.assertIsNone(eca.scrape_rank("https://www.handelsregister.de/x"))
        self.assertIsNone(eca.scrape_rank("https://handelsregister.de/x"))
        self.assertIsNone(eca.scrape_rank("https://example.com/x"))
        self.assertIsNone(eca.scrape_rank("https://www.firmenwissen.de/x"))

    def test_scrape_refuses_handelsregister_de(self):
        with self.assertRaises(ValueError):
            eca.firecrawl_scrape("https://www.handelsregister.de/x", "dummy-key")

    def test_scrape_refuses_non_allowlisted_host(self):
        with self.assertRaises(ValueError):
            eca.firecrawl_scrape("https://example.com/x", "dummy-key")


class TestQueryVariants(unittest.TestCase):
    def test_variants_built_in_order_and_deduped(self):
        v = eca.build_query_variants("Foo GmbH", "Köln", "HRB", "12345")
        self.assertEqual(v[0], "Foo GmbH Köln Handelsregister")
        self.assertEqual(v[1], "Foo GmbH Köln HRB")
        self.assertEqual(v[2], "Foo GmbH Köln Gegenstand")
        self.assertEqual(v[3], "Foo GmbH HRB 12345")

    def test_variants_handle_missing_city_without_blank_tokens(self):
        v = eca.build_query_variants("Foo GmbH", "", "HRB", "12345")
        self.assertIn("Foo GmbH Handelsregister", v)
        # no double spaces / stray blanks
        self.assertTrue(all(q == " ".join(q.split()) for q in v))

    def test_variants_default_type_to_hrb(self):
        v = eca.build_query_variants("Foo GmbH", "Köln", "", "12345")
        self.assertIn("Foo GmbH Köln HRB", v)


class TestProcessCompany(unittest.TestCase):
    def _translate(self, de):
        return (f"de:{de[:20]}", "ar:عربي")

    def test_accept_high_from_snippet(self):
        co = {"entity_id": "e1", "debtor_name": "Foo GmbH", "debtor_city": "Köln",
              "registry_court": "Köln", "registry_type": "HRB",
              "registry_number": "12345"}
        search = [{"url": "https://northdata.com/foo", "title": "Foo GmbH",
                   "description": ("Amtsgericht Köln HRB 12345. Gegenstand des "
                                   "Unternehmens: Handel mit Möbeln.")}]
        d = eca.process_company(
            co, search_fn=lambda q: search,
            scrape_fn=lambda u, stealth=False: "", translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(d.source, "aggregator")
        self.assertEqual(d.confidence, "high")
        self.assertEqual(d.matched_hrb, "12345")
        self.assertEqual(d.purpose_raw, "Handel mit Möbeln.")

    def test_pass2_scrape_recovers_truncated_purpose(self):
        co = {"entity_id": "e2", "debtor_name": "Bar GmbH", "debtor_city": "Ulm",
              "registry_court": "Ulm", "registry_type": "HRB",
              "registry_number": "555"}
        ur = "https://www.unternehmensregister.de/ureg/result.html?id=9"
        search = [{"url": ur, "title": "Bar GmbH",
                   "description": ("Amtsgericht Ulm HRB 555. Gegenstand des "
                                   "Unternehmens: Herstellung von …")}]
        page = ("Amtsgericht Ulm HRB 555\nGegenstand des Unternehmens: "
                "Herstellung von Maschinen und der Handel damit.")
        d = eca.process_company(
            co, search_fn=lambda q: search,
            scrape_fn=lambda u, stealth=False: page if u == ur else "",
            translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(d.source, "unternehmensregister")
        self.assertEqual(d.source_ref, ur)
        self.assertIn("Herstellung von Maschinen", d.purpose_raw)

    def test_reject_wrong_court(self):
        co = {"entity_id": "e3", "debtor_name": "Baz GmbH", "debtor_city": "Kassel",
              "registry_court": "Kassel", "registry_type": "HRB",
              "registry_number": "18311"}
        search = [{"url": "https://northdata.com/baz", "title": "Baz GmbH",
                   "description": "Amtsgericht München HRB 18311. Gegenstand: Bau."}]
        d = eca.process_company(
            co, search_fn=lambda q: search,
            scrape_fn=lambda u, stealth=False: "", translate_fn=self._translate)
        self.assertFalse(d.accepted)
        self.assertIn("strict gate", d.reason)

    def test_pass2_scrape_recovers_truncated_purpose_from_aggregator(self):
        # Truncated snippet on an aggregator -> pass 2 scrapes that matched page
        # and recovers the full Gegenstand. source = 'aggregator'.
        co = {"entity_id": "e4", "debtor_name": "Qux GmbH", "debtor_city": "Bonn",
              "registry_court": "Bonn", "registry_type": "HRB",
              "registry_number": "777"}
        nd = "https://northdata.com/qux"
        search = [{"url": nd, "title": "Qux GmbH",
                   "description": ("Amtsgericht Bonn HRB 777. Gegenstand des "
                                   "Unternehmens: Beratung von …")}]
        page = ("Amtsgericht Bonn HRB 777\nGegenstand des Unternehmens: "
                "Beratung von Unternehmen sowie Vermittlung von Versicherungen.")
        d = eca.process_company(
            co, search_fn=lambda q: search,
            scrape_fn=lambda u, stealth=False: page if u == nd else "",
            translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(d.source, "aggregator")
        self.assertEqual(d.source_ref, nd)
        self.assertEqual(d.pass_used, "2")
        self.assertIn("Vermittlung von Versicherungen", d.purpose_raw)

    def test_pass2_prefers_least_risk_domain(self):
        # Two identity-matched results: an aggregator (northdata) and the
        # official unternehmensregister.de. Pass 2 must scrape the official one.
        co = {"entity_id": "e5", "debtor_name": "Zeta GmbH", "debtor_city": "Ulm",
              "registry_court": "Ulm", "registry_type": "HRB",
              "registry_number": "888"}
        nd = "https://northdata.com/zeta"
        ur = "https://www.unternehmensregister.de/ureg/result.html?id=7"
        trunc = ("Amtsgericht Ulm HRB 888. Gegenstand des Unternehmens: "
                 "Herstellung von …")
        search = [
            {"url": nd, "title": "Zeta GmbH", "description": trunc},
            {"url": ur, "title": "Zeta GmbH", "description": trunc},
        ]
        scraped = {}

        def scrape(u, stealth=False):
            scraped["url"] = u
            return ("Amtsgericht Ulm HRB 888\nGegenstand des Unternehmens: "
                    "Herstellung von Bauteilen und der Handel damit.")

        d = eca.process_company(
            co, search_fn=lambda q: search,
            scrape_fn=scrape, translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(scraped["url"], ur)            # scraped the official one
        self.assertEqual(d.source, "unternehmensregister")
        self.assertEqual(d.source_ref, ur)

    def test_pass2_rejects_when_scraped_page_fails_reverification(self):
        # Identity matched on the snippet, but the scraped page shows a DIFFERENT
        # HRB -> re-verification fails -> not accepted (no false accept).
        co = {"entity_id": "e6", "debtor_name": "Mismatch GmbH",
              "debtor_city": "Bonn", "registry_court": "Bonn",
              "registry_type": "HRB", "registry_number": "777"}
        nd = "https://northdata.com/mismatch"
        search = [{"url": nd, "title": "Mismatch GmbH",
                   "description": ("Amtsgericht Bonn HRB 777. Gegenstand des "
                                   "Unternehmens: Beratung von …")}]
        wrong_page = ("Amtsgericht Bonn HRB 99999\nGegenstand des Unternehmens: "
                      "Handel mit Waren aller Art.")
        d = eca.process_company(
            co, search_fn=lambda q: search,
            scrape_fn=lambda u, stealth=False: wrong_page, translate_fn=self._translate)
        self.assertFalse(d.accepted)
        self.assertIn("pass2: HRB re-verify failed", d.reason)

    def test_pass2_absent_hrb_is_not_mismatch_accepts_medium(self):
        # Snippet truncated (pass-1 had court+num). The scraped page carries a
        # Gegenstand under a 'Tätigkeit' label but NO HRB -> absence is not a
        # mismatch: trust pass-1 identity, accept the purpose, cap confidence at
        # 'medium' since the page could not re-confirm the HRB.
        co = {"entity_id": "e9", "debtor_name": "See GmbH", "debtor_city": "Konstanz",
              "registry_court": "Freiburg", "registry_type": "HRB",
              "registry_number": "445566"}
        nd = "https://www.viaductus.de/see"
        search = [{"url": nd, "title": "See GmbH",
                   "description": ("Amtsgericht Freiburg HRB 445566. Gegenstand "
                                   "des Unternehmens: Betrieb von …")}]
        page = ("# See GmbH\nKonstanz\n\nTätigkeit: Betrieb von Restaurants und "
                "Cafés sowie Catering und die Ausrichtung von Veranstaltungen.\n"
                "Mitarbeiter: 12")
        d = eca.process_company(
            co, search_fn=lambda q: search,
            scrape_fn=lambda u, stealth=False: page, translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(d.pass_used, "2")
        self.assertEqual(d.source, "aggregator")
        self.assertEqual(d.confidence, "medium")
        self.assertIn("Catering", d.purpose_raw)

    def test_pass2_low_content_reason(self):
        co = {"entity_id": "e10", "debtor_name": "Empty GmbH",
              "debtor_city": "Bonn", "registry_court": "Bonn",
              "registry_type": "HRB", "registry_number": "777"}
        nd = "https://northdata.com/empty"
        search = [{"url": nd, "title": "Empty GmbH",
                   "description": ("Amtsgericht Bonn HRB 777. Gegenstand des "
                                   "Unternehmens: Beratung von …")}]
        d = eca.process_company(
            co, search_fn=lambda q: search,
            scrape_fn=lambda u, stealth=False: "", translate_fn=self._translate)
        self.assertFalse(d.accepted)
        self.assertIn("pass2: scrape returned no/low content", d.reason)

    def test_pass2_no_gegenstand_label_reason(self):
        co = {"entity_id": "e11", "debtor_name": "NoPurp GmbH",
              "debtor_city": "Ulm", "registry_court": "Ulm",
              "registry_type": "HRB", "registry_number": "555"}
        nd = "https://northdata.com/nopurp"
        search = [{"url": nd, "title": "NoPurp GmbH",
                   "description": ("Amtsgericht Ulm HRB 555. Gegenstand des "
                                   "Unternehmens: Herstellung von …")}]
        # HRB matches (re-verify ok) but the page has no purpose label at all.
        page = ("Amtsgericht Ulm HRB 555. Kontaktdaten und Anschrift der "
                "Gesellschaft, Telefon und Öffnungszeiten folgen weiter unten.")
        d = eca.process_company(
            co, search_fn=lambda q: search,
            scrape_fn=lambda u, stealth=False: page, translate_fn=self._translate)
        self.assertFalse(d.accepted)
        self.assertIn("pass2: no Gegenstand label found on page", d.reason)

    def test_query_variant_fallback_finds_match_on_later_variant(self):
        # First variant returns a non-matching snippet; a later variant returns
        # the HRB-bearing snippet. process_company must try variants until match.
        co = {"entity_id": "e7", "debtor_name": "Late GmbH", "debtor_city": "Köln",
              "registry_court": "Köln", "registry_type": "HRB",
              "registry_number": "12345"}
        good = "Late GmbH HRB 12345"  # the "<name> <TYPE> <number>" variant
        hit = [{"url": "https://northdata.com/late", "title": "Late GmbH",
                "description": ("Amtsgericht Köln HRB 12345. Gegenstand des "
                                "Unternehmens: Handel mit Möbeln.")}]
        miss = [{"url": "https://example.com/x", "title": "unrelated",
                 "description": "no registry token here"}]

        def search(q):
            return hit if q == good else miss

        d = eca.process_company(
            co, search_fn=search, scrape_fn=lambda u, stealth=False: "",
            translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(d.matched_variant, good)
        self.assertEqual(d.pass_used, "1")

    def test_unternehmensregister_snippet_only_is_classified_official(self):
        # Full purpose already in an unternehmensregister.de snippet (no scrape):
        # source must still be classified 'unternehmensregister' by domain.
        co = {"entity_id": "e8", "debtor_name": "Off GmbH", "debtor_city": "Köln",
              "registry_court": "Köln", "registry_type": "HRB",
              "registry_number": "222"}
        ur = "https://www.unternehmensregister.de/ureg/result.html?id=3"
        search = [{"url": ur, "title": "Off GmbH",
                   "description": ("Amtsgericht Köln HRB 222. Gegenstand des "
                                   "Unternehmens: Handel mit Textilien.")}]
        d = eca.process_company(
            co, search_fn=lambda q: search,
            scrape_fn=lambda u, stealth=False: "", translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(d.source, "unternehmensregister")
        self.assertEqual(d.pass_used, "1")


class TestTieredPass2(unittest.TestCase):
    """Tiered pass-2 scrape: cheap easy sources first, Stealth only as last resort."""

    def _translate(self, de):
        return (f"de:{de[:20]}", "ar:عربي")

    def _co(self, **kw):
        base = {"entity_id": "t", "debtor_name": "T GmbH", "debtor_city": "Ulm",
                "registry_court": "Ulm", "registry_type": "HRB",
                "registry_number": "555"}
        base.update(kw)
        return base

    def test_tier_a_recovers_slow_js_page_at_one_credit(self):
        # Normal render-scrape returns full content (slow-JS page hydrated via
        # waitFor) -> accept at Tier A, 1 normal credit, no Stealth.
        nd = "https://northdata.com/t"
        search = [{"url": nd, "title": "T GmbH",
                   "description": ("Amtsgericht Ulm HRB 555. Gegenstand des "
                                   "Unternehmens: Handel von …")}]
        page = ("Amtsgericht Ulm HRB 555\nGegenstand des Unternehmens: "
                "Handel mit Waren aller Art im In- und Ausland.")
        d = eca.process_company(
            self._co(), search_fn=lambda q: search,
            scrape_fn=lambda u, stealth=False: page, translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(d.scrape_tier, "A")
        self.assertEqual(d.normal_credits, 1)
        self.assertEqual(d.stealth_credits, 0)

    def test_tier_b_falls_back_to_other_matched_target(self):
        # Primary (least-risk) target empty; an alternative matched target has
        # content -> Tier B, two normal credits, no Stealth.
        ur = "https://www.unternehmensregister.de/x"  # rank 0 -> primary
        nd = "https://northdata.com/t"                # rank 5 -> alternative
        desc = ("Amtsgericht Ulm HRB 555. Gegenstand des Unternehmens: "
                "Herstellung von …")
        search = [{"url": nd, "title": "T GmbH", "description": desc},
                  {"url": ur, "title": "T GmbH", "description": desc}]
        good = ("Amtsgericht Ulm HRB 555\nGegenstand des Unternehmens: "
                "Herstellung von Maschinen und der Handel damit.")

        def scrape(u, stealth=False):
            return "" if u == ur else good

        d = eca.process_company(
            self._co(), search_fn=lambda q: search,
            scrape_fn=scrape, translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(d.scrape_tier, "B")
        self.assertEqual(d.source_ref, nd)
        self.assertEqual(d.normal_credits, 2)
        self.assertEqual(d.stealth_credits, 0)

    def test_tier_c_stealth_only_after_a_and_b_fail(self):
        # Single hard-aggregator match. Normal empty (A fails, no B) -> Stealth
        # recovers it -> Tier C: 1 normal + 5 stealth credits, normal tried first.
        nd = "https://northdata.com/t"
        search = [{"url": nd, "title": "T GmbH",
                   "description": ("Amtsgericht Ulm HRB 555. Gegenstand des "
                                   "Unternehmens: Im- und Export von …")}]
        page = ("Amtsgericht Ulm HRB 555\nGegenstand des Unternehmens: "
                "Im- und Export von Waren aller Art.")
        calls = []

        def scrape(u, stealth=False):
            calls.append(stealth)
            return page if stealth else ""

        d = eca.process_company(
            self._co(), search_fn=lambda q: search,
            scrape_fn=scrape, translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(d.scrape_tier, "C")
        self.assertEqual(d.normal_credits, 1)
        self.assertEqual(d.stealth_credits, 5)
        self.assertEqual(calls, [False, True])  # normal attempted before Stealth

    def test_allow_stealth_false_blocks_tier_c(self):
        nd = "https://northdata.com/t"
        search = [{"url": nd, "title": "T GmbH",
                   "description": ("Amtsgericht Ulm HRB 555. Gegenstand des "
                                   "Unternehmens: Im- und Export von …")}]
        page = ("Amtsgericht Ulm HRB 555\nGegenstand des Unternehmens: "
                "Im- und Export von Waren aller Art.")
        seen = []

        def scrape(u, stealth=False):
            seen.append(stealth)
            return page if stealth else ""

        d = eca.process_company(
            self._co(), search_fn=lambda q: search, scrape_fn=scrape,
            translate_fn=self._translate, allow_stealth=False)
        self.assertFalse(d.accepted)
        self.assertEqual(d.stealth_credits, 0)
        self.assertNotIn(True, seen)  # Stealth never invoked
        self.assertIn("pass2: scrape returned no/low content", d.reason)

    def test_mismatch_does_not_trigger_stealth(self):
        # Primary page shows a DIFFERENT HRB (wrong company) -> Stealth must not
        # run (it cannot fix wrong content) -> no Stealth credits.
        nd = "https://northdata.com/t"
        search = [{"url": nd, "title": "T GmbH",
                   "description": ("Amtsgericht Ulm HRB 555. Gegenstand des "
                                   "Unternehmens: Handel von …")}]
        wrong = ("Amtsgericht Ulm HRB 99999\nGegenstand des Unternehmens: "
                 "Etwas ganz anderes auf dieser Seite.")
        seen = []

        def scrape(u, stealth=False):
            seen.append(stealth)
            return wrong

        d = eca.process_company(
            self._co(), search_fn=lambda q: search,
            scrape_fn=scrape, translate_fn=self._translate)
        self.assertFalse(d.accepted)
        self.assertNotIn(True, seen)
        self.assertEqual(d.stealth_credits, 0)
        self.assertIn("pass2: HRB re-verify failed", d.reason)

    def test_tier_b_reapplies_identity_and_skips_mismatched_alt(self):
        # Primary empty; first alt is a different company (mismatch -> skipped);
        # second alt matches -> Tier B accept. Identity is re-checked per page.
        ur = "https://www.unternehmensregister.de/x"  # rank 0 -> empty
        hr = "https://handelsregister.ai/y"           # rank 1 -> mismatch
        nd = "https://northdata.com/z"                # rank 5 -> good
        desc = "Amtsgericht Ulm HRB 555. Gegenstand des Unternehmens: Bau von …"
        search = [{"url": ur, "title": "T", "description": desc},
                  {"url": hr, "title": "T", "description": desc},
                  {"url": nd, "title": "T", "description": desc}]
        good = ("Amtsgericht Ulm HRB 555\nGegenstand des Unternehmens: "
                "Bau von Häusern und Hochbau aller Art.")
        wrong = ("Amtsgericht Ulm HRB 12121\nGegenstand des Unternehmens: "
                 "Ein ganz anderes Unternehmen hier.")

        def scrape(u, stealth=False):
            if u == ur:
                return ""
            if u == hr:
                return wrong
            return good

        d = eca.process_company(
            self._co(), search_fn=lambda q: search,
            scrape_fn=scrape, translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(d.scrape_tier, "B")
        self.assertEqual(d.source_ref, nd)
        self.assertEqual(d.normal_credits, 3)  # ur + hr + nd, all normal cost
        self.assertEqual(d.stealth_credits, 0)


class TestVerifyHrbOnPage(unittest.TestCase):
    def test_match_tolerant_of_formatting(self):
        self.assertEqual(
            eca.verify_hrb_on_page("Amtsgericht Ulm HRB 555", "555"), "match")
        self.assertEqual(
            eca.verify_hrb_on_page("HRB 0704974 B", "704974"), "match")

    def test_match_is_type_agnostic(self):
        # 'ignore HRB/HRA prefixes' — a matching number counts regardless of type.
        self.assertEqual(eca.verify_hrb_on_page("HRA 555", "555"), "match")

    def test_mismatch_only_when_different_number_present(self):
        self.assertEqual(eca.verify_hrb_on_page("HRB 99999", "555"), "mismatch")

    def test_absence_is_not_a_mismatch(self):
        self.assertEqual(
            eca.verify_hrb_on_page("Tätigkeit: Handel mit Waren.", "555"),
            "absent")
        self.assertEqual(eca.verify_hrb_on_page("", "555"), "absent")


class TestUpsertPayload(unittest.TestCase):
    def _decision(self):
        d = eca.Decision("ent-1", "Foo GmbH")
        d.source = "aggregator"
        d.purpose_de = "de summary"
        d.purpose_ar = "ar summary"
        d.confidence = "medium"
        d.source_ref = "https://northdata.com/foo"
        d.matched_hrb = "12345"
        d.purpose_raw = "raw scraped Gegenstand text"
        return d

    def test_payload_keys_are_subset_of_live_schema(self):
        row = eca.activity_payload(self._decision())
        self.assertTrue(set(row).issubset(eca.ACTIVITY_COLUMNS))

    def test_payload_omits_purpose_raw_and_db_managed_columns(self):
        row = eca.activity_payload(self._decision())
        for col in ("purpose_raw", "id", "created_at", "updated_at"):
            self.assertNotIn(col, row)

    def test_payload_sets_expected_columns(self):
        row = eca.activity_payload(self._decision())
        self.assertIn("extracted_at", row)
        self.assertEqual(row["entity_id"], "ent-1")
        self.assertEqual(row["matched_hrb"], "12345")
        self.assertEqual(row["source"], "aggregator")

    def test_guard_fails_fast_on_unknown_column(self):
        # Simulate schema drift so a real key becomes "unknown" -> must raise.
        orig = eca.ACTIVITY_COLUMNS
        eca.ACTIVITY_COLUMNS = frozenset(orig - {"matched_hrb"})
        try:
            with self.assertRaises(RuntimeError) as ctx:
                eca.activity_payload(self._decision())
            self.assertIn("matched_hrb", str(ctx.exception))
        finally:
            eca.ACTIVITY_COLUMNS = orig


class TestEmptyMeaning(unittest.TestCase):
    """FIX 1 — reject 'no determinable activity / shell / no data' boilerplate."""

    def test_blocks_german_no_activity_phrases(self):
        self.assertTrue(eca.is_empty_meaning(
            "Kein operativer Geschäftsbetrieb feststellbar.", "نص عربي"))
        self.assertTrue(eca.is_empty_meaning("Keine Angabe.", "نص عربي"))
        self.assertTrue(eca.is_empty_meaning("Nicht ermittelbar.", "نص عربي"))

    def test_blocks_holding_shell_phrase_but_not_plain_asset_mgmt(self):
        self.assertTrue(eca.is_empty_meaning(
            "Verwaltung eigenen Vermögens ohne eigenen Geschäftsbetrieb.",
            "نشاط عربي"))
        # Plain Vermögensverwaltung is a REAL activity -> must NOT be blocked.
        self.assertFalse(eca.is_empty_meaning(
            "Vermögensverwaltung und das Halten von Beteiligungen.",
            "إدارة الأصول وحيازة الحصص."))

    def test_blocks_arabic_no_activity_phrases(self):
        self.assertTrue(eca.is_empty_meaning(
            "Handel mit Waren.", "لا يمكن تحديد أي نشاط تجاري تشغيلي للشركة."))
        self.assertTrue(eca.is_empty_meaning("Handel mit Waren.", "غير محدد"))

    def test_empty_strings_are_empty_meaning(self):
        self.assertTrue(eca.is_empty_meaning("", "نص"))
        self.assertTrue(eca.is_empty_meaning("Handel mit Waren.", ""))

    def test_real_activity_is_kept(self):
        self.assertFalse(eca.is_empty_meaning(
            "Betrieb eines Restaurants und Catering.",
            "تشغيل مطعم وتقديم خدمات الطعام."))

    def test_process_company_rejects_empty_meaning_after_translation(self):
        # Identity matches and a full purpose is parsed, but the TRANSLATION is a
        # shell phrase -> rejected, not accepted, and not stored.
        co = {"entity_id": "em1", "debtor_name": "Shell GmbH",
              "debtor_city": "Berlin", "registry_court": "Charlottenburg",
              "registry_type": "HRB", "registry_number": "224488"}
        search = [{"url": "https://northdata.com/shell", "title": "Shell GmbH",
                   "description": ("Amtsgericht Charlottenburg HRB 224488. "
                                   "Gegenstand des Unternehmens: Verwaltung "
                                   "eigenen Vermögens ohne eigenen "
                                   "Geschäftsbetrieb.")}]

        def translate(de):
            return (de, "لا يمكن تحديد أي نشاط")

        d = eca.process_company(
            co, search_fn=lambda q: search,
            scrape_fn=lambda u, stealth=False: "", translate_fn=translate)
        self.assertFalse(d.accepted)
        self.assertEqual(d.reason, eca.EMPTY_MEANING_REASON)
        # matched_hrb kept for the log; row is NOT built/stored.
        self.assertEqual(d.matched_hrb, "224488")


class TestEnvDefaults(unittest.TestCase):
    def test_allow_stealth_defaults_off(self):
        # FIX 2 — Stealth is OFF unless ALLOW_STEALTH is explicitly truthy.
        saved = os.environ.pop("ALLOW_STEALTH", None)
        try:
            self.assertFalse(eca.truthy(os.environ.get("ALLOW_STEALTH", "0")))
        finally:
            if saved is not None:
                os.environ["ALLOW_STEALTH"] = saved

    def test_default_order_is_deterministic_and_allowed(self):
        self.assertEqual(eca.DEFAULT_ORDER, "entity_id.asc")
        self.assertIn(eca.DEFAULT_ORDER, eca.ALLOWED_ORDERS)


class TestFetchCandidatesResume(unittest.TestCase):
    """FIX 3 — reruns resume by excluding already-enriched entities; batch size."""

    def _patch_postgrest(self, view_rows, existing):
        captured = {"view_path": None}

        def fake(method, path, base, key, body=None, prefer=None):
            if path.startswith(eca.ACTIVITY_TABLE):
                return existing
            captured["view_path"] = path
            return view_rows

        return fake, captured

    def test_skips_already_enriched_and_honors_batch_size(self):
        view_rows = [{"entity_id": f"e{i}", "debtor_name": f"C{i}"}
                     for i in range(6)]
        existing = [{"entity_id": "e1"}, {"entity_id": "e3"}]
        fake, _ = self._patch_postgrest(view_rows, existing)
        orig = eca.postgrest
        eca.postgrest = fake
        try:
            out = eca.fetch_candidates("b", "k", max_companies=2, fetch_limit=100)
        finally:
            eca.postgrest = orig
        ids = [r["entity_id"] for r in out]
        self.assertNotIn("e1", ids)        # already enriched -> skipped
        self.assertNotIn("e3", ids)
        self.assertEqual(len(out), 2)      # batch size (MAX_COMPANIES) honored
        self.assertEqual(ids, ["e0", "e2"])

    def test_invalid_order_falls_back_to_default(self):
        fake, captured = self._patch_postgrest([], [])
        orig = eca.postgrest
        eca.postgrest = fake
        try:
            eca.fetch_candidates("b", "k", 5, 100, order="; DROP TABLE x")
        finally:
            eca.postgrest = orig
        self.assertIn(f"order={eca.DEFAULT_ORDER}", captured["view_path"])

    def test_valid_order_is_used(self):
        fake, captured = self._patch_postgrest([], [])
        orig = eca.postgrest
        eca.postgrest = fake
        try:
            eca.fetch_candidates("b", "k", 5, 100, order="announcement_date.desc")
        finally:
            eca.postgrest = orig
        self.assertIn("order=announcement_date.desc", captured["view_path"])


class TestAttemptsLedger(unittest.TestCase):
    """The attempts ledger that makes the backfill ADVANCE past rejected heads."""

    def test_parse_ts_handles_z_and_offset_and_blank(self):
        self.assertIsNotNone(eca.parse_ts("2026-06-30T00:00:00Z"))
        self.assertIsNotNone(eca.parse_ts("2026-06-30T00:00:00+00:00"))
        self.assertIsNone(eca.parse_ts(""))
        self.assertIsNone(eca.parse_ts("not-a-date"))

    def test_recently_attempted_window(self):
        now = datetime(2026, 6, 30, tzinfo=timezone.utc)
        amap = {
            "fresh": {"attempts": 1,
                      "last_attempt_at": (now - timedelta(days=5)).isoformat()},
            "stale": {"attempts": 2,
                      "last_attempt_at": (now - timedelta(days=40)).isoformat()},
            "blank": {"attempts": 1, "last_attempt_at": ""},
        }
        recent = eca.recently_attempted(amap, 30, now)
        self.assertEqual(recent, {"fresh"})  # stale (40d) is retryable again

    def test_retry_after_zero_disables_window(self):
        now = datetime(2026, 6, 30, tzinfo=timezone.utc)
        amap = {"fresh": {"attempts": 1,
                          "last_attempt_at": now.isoformat()}}
        self.assertEqual(eca.recently_attempted(amap, 0, now), set())

    def test_load_attempts_graceful_when_table_absent(self):
        def boom(*a, **k):
            raise RuntimeError("relation \"company_activity_attempts\" does not exist")

        orig = eca.postgrest
        eca.postgrest = boom
        try:
            amap, ok = eca.load_attempts("b", "k")
        finally:
            eca.postgrest = orig
        self.assertEqual(amap, {})
        self.assertFalse(ok)

    def test_load_attempts_parses_rows(self):
        rows = [{"entity_id": "e1", "attempts": 3,
                 "last_attempt_at": "2026-06-01T00:00:00+00:00"}]

        def fake(method, path, base, key, body=None, prefer=None):
            return rows

        orig = eca.postgrest
        eca.postgrest = fake
        try:
            amap, ok = eca.load_attempts("b", "k")
        finally:
            eca.postgrest = orig
        self.assertTrue(ok)
        self.assertEqual(amap["e1"]["attempts"], 3)

    def test_upsert_attempt_builds_minimal_idempotent_row(self):
        captured = {}

        def fake(method, path, base, key, body=None, prefer=None):
            captured.update(path=path, body=body, prefer=prefer)
            return None

        orig = eca.postgrest
        eca.postgrest = fake
        try:
            eca.upsert_attempt("b", "k", "e9", 4, "some reason")
        finally:
            eca.postgrest = orig
        row = captured["body"][0]
        self.assertEqual(set(row), {"entity_id", "attempts",
                                    "last_attempt_at", "last_reason"})
        self.assertEqual(row["entity_id"], "e9")
        self.assertEqual(row["attempts"], 4)
        self.assertIn("on_conflict=entity_id", captured["path"])
        self.assertIn("merge-duplicates", captured["prefer"])


class TestFetchCandidatesAdvance(unittest.TestCase):
    """fetch_candidates must skip recently-attempted AND advance across pages."""

    def test_excludes_recently_attempted_and_selects_new(self):
        view_rows = [{"entity_id": f"e{i}", "debtor_name": f"C{i}"}
                     for i in range(6)]
        existing = [{"entity_id": "e0"}, {"entity_id": "e1"}]

        def fake(method, path, base, key, body=None, prefer=None):
            if path.startswith(eca.ACTIVITY_TABLE):
                return existing
            return view_rows  # < page size -> single page

        stats: dict = {}
        orig = eca.postgrest
        eca.postgrest = fake
        try:
            out = eca.fetch_candidates("b", "k", max_companies=2, fetch_limit=100,
                                       exclude={"e2", "e3"}, stats=stats)
        finally:
            eca.postgrest = orig
        ids = [r["entity_id"] for r in out]
        self.assertEqual(ids, ["e4", "e5"])      # advanced past enriched + recent
        self.assertEqual(stats["skipped_recent"], 2)

    def test_pagination_advances_past_attempted_head(self):
        # The bug: the entire head is unrecoverable. With small pages, selection
        # must page THROUGH the attempted head to reach fresh companies.
        all_rows = [{"entity_id": f"e{i}", "debtor_name": f"C{i}"}
                    for i in range(6)]

        def fake(method, path, base, key, body=None, prefer=None):
            if path.startswith(eca.ACTIVITY_TABLE):
                return []
            off = int(re.search(r"offset=(\d+)", path).group(1))
            lim = int(re.search(r"limit=(\d+)", path).group(1))
            return all_rows[off:off + lim]

        stats: dict = {}
        orig = eca.postgrest
        eca.postgrest = fake
        try:
            out = eca.fetch_candidates(
                "b", "k", max_companies=2, fetch_limit=2,   # 2 rows per page
                exclude={"e0", "e1", "e2", "e3"},           # whole head attempted
                stats=stats)
        finally:
            eca.postgrest = orig
        ids = [r["entity_id"] for r in out]
        self.assertEqual(ids, ["e4", "e5"])     # reached fresh companies on page 3
        self.assertEqual(stats["skipped_recent"], 4)
        self.assertGreaterEqual(stats["scanned"], 6)


class TestSerpApi(unittest.TestCase):
    """PASS 0 — SerpApi snippets reuse the existing gate/filter/translate, no scrape."""

    def _translate(self, de):
        return (f"de:{de[:20]}", "ar:عربي")

    def _echo_translate(self, de):
        # Echoes the German purpose (used to exercise the empty-meaning filter).
        return (de, "نشاط")

    def _co(self, court="Köln", num="12345"):
        return {"entity_id": "s1", "debtor_name": "Foo GmbH", "debtor_city": "Köln",
                "registry_court": court, "registry_type": "HRB",
                "registry_number": num}

    def _no_scrape(self, url, stealth=False):
        raise AssertionError("SerpApi path must never scrape")

    def test_serpapi_search_normalizes_organic_results(self):
        captured = {}

        def fake_http_json(method, url, headers, body=None, timeout=60):
            captured["method"] = method
            captured["url"] = url
            return 200, {"organic_results": [
                {"link": "https://northdata.com/x", "title": "Foo GmbH",
                 "snippet": "Amtsgericht Köln HRB 12345. Gegenstand: Handel.",
                 "snippet_highlighted_words": ["HRB", "12345"]},
                {"title": "no link"},  # missing link/snippet tolerated
            ]}

        orig = eca.http_json
        eca.http_json = fake_http_json
        try:
            out = eca.serpapi_search("Foo GmbH Köln Handelsregister", "KEY")
        finally:
            eca.http_json = orig
        self.assertEqual(captured["method"], "GET")
        self.assertIn("engine=google", captured["url"])
        self.assertIn("hl=de", captured["url"])
        self.assertEqual(out[0]["url"], "https://northdata.com/x")
        self.assertIn("Amtsgericht Köln HRB 12345", out[0]["description"])
        self.assertEqual(out[1]["url"], "")  # tolerant of missing fields

    def test_serpapi_search_raises_on_non_200(self):
        def fake(method, url, headers, body=None, timeout=60):
            return 401, {"error": "invalid key"}

        orig = eca.http_json
        eca.http_json = fake
        try:
            with self.assertRaises(RuntimeError):
                eca.serpapi_search("q", "BAD")
        finally:
            eca.http_json = orig

    def test_serpapi_accepts_complete_snippet_without_scraping(self):
        serp = [{"url": "https://northdata.com/foo", "title": "Foo GmbH",
                 "description": ("Amtsgericht Köln HRB 12345. Gegenstand des "
                                 "Unternehmens: Handel mit Möbeln.")}]
        d = eca.process_company(
            self._co(), serpapi_fn=lambda q: serp,
            scrape_fn=self._no_scrape, translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(d.provider, "serpapi")
        self.assertEqual(d.source, "aggregator")   # SerpApi rows are always aggregator
        self.assertEqual(d.pass_used, "1")
        self.assertEqual(d.scrape_tier, "")         # never scraped
        self.assertEqual(d.serpapi_searches, 1)
        self.assertEqual(d.credits, 0)              # no Firecrawl credits spent
        self.assertEqual(d.purpose_raw, "Handel mit Möbeln.")

    def test_serpapi_identity_gate_rejects_wrong_court(self):
        # Same gate as Firecrawl: wrong court for the same HRB -> reject.
        serp = [{"url": "https://northdata.com/x", "title": "Foo GmbH",
                 "description": ("Amtsgericht München HRB 18311. Gegenstand des "
                                 "Unternehmens: Hoch- und Tiefbau.")}]
        d = eca.process_company(
            self._co(court="Kassel", num="18311"), serpapi_fn=lambda q: serp,
            scrape_fn=self._no_scrape, translate_fn=self._translate)
        self.assertFalse(d.accepted)
        self.assertIn("strict gate", d.reason)
        self.assertEqual(d.serpapi_searches, 1)

    def test_serpapi_empty_meaning_filter_applies(self):
        # Complete snippet but a holding-shell purpose -> empty-meaning reject.
        serp = [{"url": "https://northdata.com/shell", "title": "Foo GmbH",
                 "description": ("Amtsgericht Köln HRB 12345. Gegenstand des "
                                 "Unternehmens: Verwaltung eigenen Vermögens "
                                 "ohne eigenen Geschäftsbetrieb.")}]
        d = eca.process_company(
            self._co(), serpapi_fn=lambda q: serp,
            scrape_fn=self._no_scrape, translate_fn=self._echo_translate)
        self.assertFalse(d.accepted)
        self.assertEqual(d.reason, eca.EMPTY_MEANING_REASON)
        self.assertEqual(d.provider, "serpapi")

    def test_serpapi_truncated_falls_through_to_firecrawl(self):
        # SerpApi snippet matches identity but purpose is truncated -> SerpApi
        # does NOT win (no scraping); Firecrawl provides a complete snippet.
        serp = [{"url": "https://northdata.com/foo", "title": "Foo GmbH",
                 "description": ("Amtsgericht Köln HRB 12345. Gegenstand des "
                                 "Unternehmens: Herstellung von …")}]
        fire = [{"url": "https://northdata.com/foo2", "title": "Foo GmbH",
                 "description": ("Amtsgericht Köln HRB 12345. Gegenstand des "
                                 "Unternehmens: Herstellung von Maschinen.")}]
        d = eca.process_company(
            self._co(), serpapi_fn=lambda q: serp, search_fn=lambda q: fire,
            scrape_fn=self._no_scrape, translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(d.provider, "firecrawl")
        self.assertEqual(d.serpapi_searches, 1)
        self.assertEqual(d.credits, 2)   # one Firecrawl search

    def test_serpapi_only_truncated_is_rejected_and_recordable(self):
        # SerpApi truncated + no Firecrawl provider -> reject, but the Decision
        # still carries entity_id + reason so main() records the attempt (advance).
        serp = [{"url": "https://northdata.com/foo", "title": "Foo GmbH",
                 "description": ("Amtsgericht Köln HRB 12345. Gegenstand des "
                                 "Unternehmens: Herstellung von …")}]
        d = eca.process_company(
            self._co(), serpapi_fn=lambda q: serp, translate_fn=self._translate)
        self.assertFalse(d.accepted)
        self.assertEqual(d.serpapi_searches, 1)
        self.assertTrue(d.entity_id)
        self.assertTrue(d.reason)        # recordable in company_activity_attempts


class TestSerper(unittest.TestCase):
    """PASS 0 — Serper.dev snippets reuse the same gate/filter, no scraping."""

    def _translate(self, de):
        return (f"de:{de[:20]}", "ar:عربي")

    def _co(self, court="Köln", num="12345"):
        return {"entity_id": "r1", "debtor_name": "Foo GmbH", "debtor_city": "Köln",
                "registry_court": court, "registry_type": "HRB",
                "registry_number": num}

    def _no_scrape(self, url, stealth=False):
        raise AssertionError("Serper path must never scrape")

    def test_serper_search_posts_with_apikey_header_and_normalizes(self):
        captured = {}

        def fake_http_json(method, url, headers, body=None, timeout=60):
            captured.update(method=method, url=url, headers=headers, body=body)
            return 200, {"organic": [
                {"link": "https://northdata.com/x", "title": "Foo GmbH",
                 "snippet": "Amtsgericht Köln HRB 12345. Gegenstand: Handel."},
                {"title": "no link"},  # tolerate missing link/snippet
            ]}

        orig = eca.http_json
        eca.http_json = fake_http_json
        try:
            out = eca.serper_search("Foo GmbH Köln Handelsregister", "KEY")
        finally:
            eca.http_json = orig
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], "https://google.serper.dev/search")
        self.assertEqual(captured["headers"].get("X-API-KEY"), "KEY")
        self.assertEqual(captured["body"]["q"], "Foo GmbH Köln Handelsregister")
        self.assertEqual(out[0]["url"], "https://northdata.com/x")
        self.assertIn("HRB 12345", out[0]["description"])
        self.assertEqual(out[1]["url"], "")

    def test_serper_search_raises_on_non_200(self):
        def fake(method, url, headers, body=None, timeout=60):
            return 403, {"message": "forbidden"}

        orig = eca.http_json
        eca.http_json = fake
        try:
            with self.assertRaises(RuntimeError):
                eca.serper_search("q", "BAD")
        finally:
            eca.http_json = orig

    def test_serper_accepts_complete_snippet_without_scraping(self):
        serp = [{"url": "https://northdata.com/foo", "title": "Foo GmbH",
                 "description": ("Amtsgericht Köln HRB 12345. Gegenstand des "
                                 "Unternehmens: Handel mit Möbeln.")}]
        d = eca.process_company(
            self._co(), serper_fn=lambda q: serp,
            scrape_fn=self._no_scrape, translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(d.provider, "serper")
        self.assertEqual(d.source, "aggregator")
        self.assertEqual(d.pass_used, "1")
        self.assertEqual(d.scrape_tier, "")
        self.assertEqual(d.serper_searches, 1)
        self.assertEqual(d.serpapi_searches, 0)
        self.assertEqual(d.credits, 0)

    def test_serper_identity_gate_rejects_wrong_court(self):
        serp = [{"url": "https://northdata.com/x", "title": "Foo GmbH",
                 "description": ("Amtsgericht München HRB 18311. Gegenstand des "
                                 "Unternehmens: Hoch- und Tiefbau.")}]
        d = eca.process_company(
            self._co(court="Kassel", num="18311"), serper_fn=lambda q: serp,
            scrape_fn=self._no_scrape, translate_fn=self._translate)
        self.assertFalse(d.accepted)
        self.assertIn("strict gate", d.reason)
        self.assertEqual(d.serper_searches, 1)

    def test_serper_empty_meaning_filter_applies(self):
        serp = [{"url": "https://northdata.com/shell", "title": "Foo GmbH",
                 "description": ("Amtsgericht Köln HRB 12345. Gegenstand des "
                                 "Unternehmens: Verwaltung eigenen Vermögens "
                                 "ohne eigenen Geschäftsbetrieb.")}]
        d = eca.process_company(
            self._co(), serper_fn=lambda q: serp, scrape_fn=self._no_scrape,
            translate_fn=lambda de: (de, "نشاط"))
        self.assertFalse(d.accepted)
        self.assertEqual(d.reason, eca.EMPTY_MEANING_REASON)
        self.assertEqual(d.provider, "serper")


class TestBuildSummary(unittest.TestCase):
    def test_summary_has_required_keys_and_coverage(self):
        rr = Counter()
        rr["no snippet matched registry_number/court (strict gate)"] = 10
        rr[eca.EMPTY_MEANING_REASON] = 2
        s = eca.build_summary(
            date="2026-06-30", providers_used=["serper"], processed=50,
            serper_searches=50, accepted=20, written=20, rejected=30,
            reject_reasons=rr, high=8, medium=12, total_enriched_now=1179,
            eligible_total=2900)
        for k in ("date", "providers_used", "companies_processed",
                  "serper_searches_used", "accepted", "written", "rejected",
                  "rejected_by_reason", "high", "medium", "total_enriched_now",
                  "eligible_total", "coverage_percent", "ok"):
            self.assertIn(k, s)
        self.assertEqual(s["companies_processed"], 50)
        self.assertEqual(s["serper_searches_used"], 50)
        self.assertEqual(s["coverage_percent"], round(100 * 1179 / 2900, 1))
        self.assertIsInstance(s["rejected_by_reason"], list)
        self.assertEqual(s["rejected_by_reason"][0][0],
                         "no snippet matched registry_number/court (strict gate)")
        json.dumps(s)  # must be JSON-serializable

    def test_summary_zero_eligible_is_safe(self):
        s = eca.build_summary(
            date="d", providers_used=[], processed=0, serper_searches=0,
            accepted=0, written=0, rejected=0, reject_reasons=Counter(),
            high=0, medium=0, total_enriched_now=0, eligible_total=0)
        self.assertEqual(s["coverage_percent"], 0.0)
        self.assertTrue(s["ok"])


if __name__ == "__main__":
    unittest.main()
