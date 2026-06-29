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


if __name__ == "__main__":
    unittest.main()
