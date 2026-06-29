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

import os
import sys
import unittest

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
            scrape_fn=lambda u: "", translate_fn=self._translate)
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
            scrape_fn=lambda u: page if u == ur else "",
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
            scrape_fn=lambda u: "", translate_fn=self._translate)
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
            scrape_fn=lambda u: page if u == nd else "",
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

        def scrape(u):
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
            scrape_fn=lambda u: wrong_page, translate_fn=self._translate)
        self.assertFalse(d.accepted)
        self.assertIn("re-verify", d.reason)

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
            co, search_fn=search, scrape_fn=lambda u: "",
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
            scrape_fn=lambda u: "", translate_fn=self._translate)
        self.assertTrue(d.accepted)
        self.assertEqual(d.source, "unternehmensregister")
        self.assertEqual(d.pass_used, "1")


if __name__ == "__main__":
    unittest.main()
