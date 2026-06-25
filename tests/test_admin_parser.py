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

from scraper import (  # noqa: E402
    parse_insolvency_admin,
    extract_fields_from_text,
    InsolvencyRecord,
)


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


class TestZumBestelltColonForms(unittest.TestCase):
    """Phase 0046: the canonical opening form
    'Zum [vorläufigen] Insolvenzverwalter wird bestellt: Rechtsanwalt NAME,
    Street, PLZ City' ended with a debtor clause / '(§ …)' / period, which the
    old (mandatory) stop set did not match, so the whole block was lost."""

    def test_vorlaeufig_ends_with_paragraph_ref(self):
        r = parse_insolvency_admin(
            "Zum vorläufigen Insolvenzverwalter wird bestellt: Rechtsanwalt "
            "Rüdiger Wienberg, Düsseldorfer Straße 38, 10707 Berlin. Verfügungen "
            "der Schuldnerin sind nur mit Zustimmung wirksam (§ 21 Abs. 2 Nr. 2 InsO)."
        )
        self.assertEqual(r["name"], "Rechtsanwalt Rüdiger Wienberg")
        self.assertEqual(r["address"], "Düsseldorfer Straße 38, 10707 Berlin")

    def test_plain_ends_with_debtor_clause(self):
        r = parse_insolvency_admin(
            "Zum Insolvenzverwalter wird bestellt: Rechtsanwalt Christoph G. Rapp, "
            "Friedrich-Ebert-Anlage 24, 69117 Heidelberg Der Schuldnerin wird die "
            "Verfügung über ihr Vermögen verboten."
        )
        self.assertEqual(r["name"], "Rechtsanwalt Christoph G. Rapp")
        self.assertEqual(r["address"], "Friedrich-Ebert-Anlage 24, 69117 Heidelberg")

    def test_co_firm_with_contact(self):
        r = parse_insolvency_admin(
            "Zum Insolvenzverwalter wird bestellt: Rechtsanwalt Sebastian Netzel, "
            "c/o Brinkmann & Partner, Colmarer Straße 5, 60528 Frankfurt am Main, "
            "Tel.: 069/37 0022-0, Fax: 069/37 0022-111. Der Schuldnerin wird die "
            "Verfügung verboten."
        )
        self.assertEqual(r["name"], "Rechtsanwalt Sebastian Netzel")
        self.assertEqual(r["firm"], "Brinkmann & Partner")
        self.assertEqual(r["address"], "Colmarer Straße 5, 60528 Frankfurt am Main")
        self.assertEqual(r["phone"], "069/37 0022-0")

    def test_block_at_end_of_text(self):
        r = parse_insolvency_admin(
            "Zum Insolvenzverwalter wird bestellt: Rechtsanwalt Max Mustermann, "
            "Hauptstr 1, 12345 Stadt."
        )
        self.assertEqual(r["name"], "Rechtsanwalt Max Mustermann")
        self.assertEqual(r["address"], "Hauptstr 1, 12345 Stadt")


class TestContactOnlyFallback(unittest.TestCase):
    """Phase 0044: when no administrator NAME can be anchored but a labelled
    email/phone is present AND an administrator role is mentioned, return the
    contact (administrator's office) so outreach data is not lost. name/firm/
    address stay None (never guessed)."""

    def test_contact_returned_without_name_when_role_present(self):
        # A follow-up announcement (e.g. Berichtstermin) that references the
        # already-appointed administrator only by contact, with no parseable
        # appointment-name structure.
        text = (
            "Im Insolvenzverfahren wird Berichtstermin bestimmt. Rückfragen an "
            "den Insolvenzverwalter unter Tel.: 030 9988776, "
            "E-Mail: kanzlei@example.de."
        )
        r = parse_insolvency_admin(text)
        self.assertIsNone(r["name"])
        self.assertIsNone(r["firm"])
        self.assertIsNone(r["address"])
        self.assertEqual(r["phone"], "030 9988776")
        self.assertEqual(r["email"], "kanzlei@example.de")

    def test_no_role_does_not_grab_unrelated_contact(self):
        # An email present but NO administrator role anywhere -> nothing, so we
        # never attribute a court/debtor address as administrator contact.
        text = "Allgemeine Mitteilung des Gerichts. E-Mail: poststelle@ag.example.de"
        r = parse_insolvency_admin(text)
        self.assertEqual(
            r, {"name": None, "firm": None, "address": None, "phone": None, "email": None}
        )

    def test_address_never_returned_without_name(self):
        # A PLZ/street is present but no name anchor -> address must stay None
        # (could be the debtor's address); only labelled email/phone are safe.
        text = (
            "Sicherungsmaßnahmen werden angeordnet. Vorläufiger "
            "Insolvenzverwalter erreichbar Tel.: 089 12345. Schuldner wohnhaft "
            "Beispielweg 4, 80331 München."
        )
        r = parse_insolvency_admin(text)
        self.assertIsNone(r["address"])
        self.assertEqual(r["phone"], "089 12345")


class TestAdminMapping(unittest.TestCase):
    """Phase 0044: the five admin fields flow through extract_fields_from_text
    and InsolvencyRecord.to_db_dict (previously firm/address/phone/email were
    dropped before persistence)."""

    FULL = (
        "Insolvenzverwalter ist: Rechtsanwalt Thomas Wilhelm, Jost Nowak "
        "Rechtsanwälte, Homburger Landstr. 838, 60437 Frankfurt am Main, "
        "Tel.: 069-209 739 0, E-Mail: wilhelm@jruc.de. Die Gläubiger werden "
        "aufgefordert:"
    )

    def test_extract_fields_returns_all_five(self):
        f = extract_fields_from_text(self.FULL)
        self.assertEqual(f["insolvency_administrator"], "Rechtsanwalt Thomas Wilhelm")
        self.assertEqual(f["insolvency_admin_firm"], "Jost Nowak Rechtsanwälte")
        self.assertEqual(f["insolvency_admin_address"],
                         "Homburger Landstr. 838, 60437 Frankfurt am Main")
        self.assertEqual(f["insolvency_admin_phone"], "069-209 739 0")
        self.assertEqual(f["insolvency_admin_email"], "wilhelm@jruc.de")

    def test_to_db_dict_includes_subfields(self):
        f = extract_fields_from_text(self.FULL)
        r = InsolvencyRecord(court="AG Frankfurt", case_number="1 IN 2/26",
                             announcement_date="2026-01-01")
        r.insolvency_administrator = f["insolvency_administrator"]
        r.insolvency_admin_firm = f["insolvency_admin_firm"]
        r.insolvency_admin_address = f["insolvency_admin_address"]
        r.insolvency_admin_phone = f["insolvency_admin_phone"]
        r.insolvency_admin_email = f["insolvency_admin_email"]
        d = r.to_db_dict()
        self.assertEqual(d["insolvency_admin_firm"], "Jost Nowak Rechtsanwälte")
        self.assertEqual(d["insolvency_admin_address"],
                         "Homburger Landstr. 838, 60437 Frankfurt am Main")
        self.assertEqual(d["insolvency_admin_phone"], "069-209 739 0")
        self.assertEqual(d["insolvency_admin_email"], "wilhelm@jruc.de")


if __name__ == "__main__":
    unittest.main()
