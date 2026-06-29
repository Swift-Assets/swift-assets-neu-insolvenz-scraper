"""Unit tests for the company-activity (Gegenstand) enrichment gate.

These guard the STRICT identity match gate that gave zero false accepts over the
42 spike companies: a purpose is accepted ONLY when a snippet's registry number
matches (type-aware) and — when a court is also visible in that snippet — the
court matches too. An explicit court mismatch (same name, different company) must
reject. We also cover the pass-2 path (truncated snippet -> scrape the matched
unternehmensregister.de page) and the refusal to scrape any other host.

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

    def test_scrape_refuses_non_unternehmensregister(self):
        with self.assertRaises(ValueError):
            eca.firecrawl_scrape_unternehmensregister(
                "https://northdata.com/x", "dummy-key")


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

    def test_truncated_without_unternehmensregister_is_not_accepted(self):
        co = {"entity_id": "e4", "debtor_name": "Qux GmbH", "debtor_city": "Bonn",
              "registry_court": "Bonn", "registry_type": "HRB",
              "registry_number": "777"}
        # Identity matches but purpose is truncated and only an aggregator result
        # exists -> we refuse to scrape it, so no usable purpose -> not accepted.
        search = [{"url": "https://northdata.com/qux", "title": "Qux GmbH",
                   "description": ("Amtsgericht Bonn HRB 777. Gegenstand des "
                                   "Unternehmens: Beratung von …")}]
        d = eca.process_company(
            co, search_fn=lambda q: search,
            scrape_fn=lambda u: "", translate_fn=self._translate)
        self.assertFalse(d.accepted)


if __name__ == "__main__":
    unittest.main()
