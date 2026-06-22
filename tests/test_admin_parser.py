"""Unit tests for parse_insolvency_admin() — Insolvenzverwalter extraction.

Texts are real announcement bodies pulled from
swift_v2.source_neu_insolvenz_announcements (project hqyktreytsjeirlpnnyr),
covering the two dominant phrasings plus c/o firms, the "wird bestellt" form
where the name runs into the street, and negative cases (boilerplate / debtor)
that must NOT yield an administrator.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import parse_insolvency_admin  # noqa: E402


class TestAdminParser(unittest.TestCase):
    def test_ist_colon_with_firm_and_email(self):
        text = (
            "... ist am 16.06.2026 um 08:51 Uhr das Insolvenzverfahren eröffnet "
            "worden. Insolvenzverwalter ist: Rechtsanwalt Thomas Wilhelm, Jost "
            "Nowak Rechtsanwälte, Homburger Landstr. 838, 60437 Frankfurt am\n"
            "Main, Tel.: 069-209 739 0, E-Mail: wilhelm@jruc.de. Die Gläubiger "
            "werden aufgefordert: ..."
        )
        r = parse_insolvency_admin(text)
        self.assertEqual(r["name"], "Rechtsanwalt Thomas Wilhelm")
        self.assertEqual(r["firm"], "Jost Nowak Rechtsanwälte")
        self.assertEqual(r["address"], "Homburger Landstr. 838, 60437 Frankfurt am Main")
        self.assertEqual(r["phone"], "069-209 739 0")
        self.assertEqual(r["email"], "wilhelm@jruc.de")

    def test_ist_colon_no_firm_no_email(self):
        text = (
            "Insolvenzverwalter ist: Rechtsanwalt Dr. Robert Schiebe, "
            "Hindenburgstraße 32, 55118 Mainz, Tel.: 06131/619230, "
            "Fax: 06131/6192311.\n\nDie Gläubiger werden aufgefordert:"
        )
        r = parse_insolvency_admin(text)
        self.assertEqual(r["name"], "Rechtsanwalt Dr. Robert Schiebe")
        self.assertIsNone(r["firm"])
        self.assertEqual(r["address"], "Hindenburgstraße 32, 55118 Mainz")
        self.assertEqual(r["phone"], "06131/619230")
        self.assertIsNone(r["email"])

    def test_ist_colon_with_c_o_firm(self):
        text = (
            "Insolvenzverwalter ist: Rechtsanwalt Jan Kind, c/o RAe. Esche - "
            "Schümann - Commichau, Am Markt 1, 28195 Bremen, Tel.: "
            "0421/17876-5.\n\nDie Gläubiger werden aufgefordert:"
        )
        r = parse_insolvency_admin(text)
        self.assertEqual(r["name"], "Rechtsanwalt Jan Kind")
        self.assertEqual(r["firm"], "RAe. Esche - Schümann - Commichau")
        self.assertEqual(r["address"], "Am Markt 1, 28195 Bremen")
        self.assertEqual(r["phone"], "0421/17876-5")

    def test_zum_bestellt_name_glued_to_street(self):
        text = (
            "Zum Insolvenzverwalter wird bestellt: Rechtsanwalt Dirk Götze "
            "Bahnhofstraße 3, 99084 Erfurt Telefon: 0361 5115070 Telefax: "
            "0361 51150720 Email: dirk.goetze@frh-erfurt.de 4. Die "
            "Insolvenzgläubiger werden aufgefordert, ..."
        )
        r = parse_insolvency_admin(text)
        self.assertEqual(r["name"], "Rechtsanwalt Dirk Götze")
        self.assertEqual(r["address"], "Bahnhofstraße 3, 99084 Erfurt")
        self.assertEqual(r["phone"], "0361 5115070")
        self.assertEqual(r["email"], "dirk.goetze@frh-erfurt.de")

    def test_zur_insolvenzverwalterin_no_email(self):
        text = (
            "Zur Insolvenzverwalterin wird bestellt: Rechtsanwältin Beate "
            "Thompson Werftstraße 9, 23730 Neustadt in Holstein Telefon: "
            "04561 5198-0 Telefax: 04561 5198-98 4. Die Insolvenzgläubiger "
            "werden aufgefordert, ..."
        )
        r = parse_insolvency_admin(text)
        self.assertEqual(r["name"], "Rechtsanwältin Beate Thompson")
        self.assertEqual(r["address"], "Werftstraße 9, 23730 Neustadt in Holstein")
        self.assertEqual(r["phone"], "04561 5198-0")
        self.assertIsNone(r["email"])

    def test_does_not_capture_boilerplate(self):
        text = (
            "Über das Vermögen der Muster Handels GmbH ist das "
            "Insolvenzverfahren eröffnet worden. Der Insolvenzverwalter ist "
            "berechtigt und verpflichtet, die Forderungen zu prüfen. Die "
            "Gläubiger werden aufgefordert:"
        )
        r = parse_insolvency_admin(text)
        self.assertIsNone(r["name"])

    def test_does_not_capture_debtor(self):
        # Debtor appears after "Über das Vermögen der ..."; the administrator is
        # a different, titled person in the appointment block.
        text = (
            "Über das Vermögen der Schmidt Bau GmbH, Hauptstraße 5, 10115 "
            "Berlin, wird das Insolvenzverfahren eröffnet. Insolvenzverwalter "
            "ist: Rechtsanwalt Klaus Meier, Lindenallee 2, 20095 Hamburg, "
            "Tel.: 040/123456. Die Gläubiger werden aufgefordert:"
        )
        r = parse_insolvency_admin(text)
        self.assertEqual(r["name"], "Rechtsanwalt Klaus Meier")
        self.assertNotIn("Schmidt", r["name"])
        self.assertEqual(r["address"], "Lindenallee 2, 20095 Hamburg")

    def test_no_administrator_returns_all_none(self):
        text = "Die Gläubiger werden aufgefordert, ihre Forderungen anzumelden."
        r = parse_insolvency_admin(text)
        self.assertEqual(
            r, {"name": None, "firm": None, "address": None, "phone": None, "email": None}
        )

    def test_empty_and_none(self):
        for v in (None, "", "   "):
            r = parse_insolvency_admin(v)
            self.assertIsNone(r["name"])


if __name__ == "__main__":
    unittest.main()
