"""Phase 0037D — tests for robust announcement-type extraction and the
source-identity / provenance fields.

All fixtures are ANONYMIZED: company examples only (e.g. "Muster GmbH"), no
real natural-person names, addresses or case numbers. Run with:

    python -m unittest discover -s tests
(stdlib unittest only — no extra test dependency required.)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import (  # noqa: E402
    InsolvencyRecord,
    detect_announcement_type,
    detect_announcement_type_full,
    extract_fields_from_text,
    compute_coverage_metrics,
)


class TestAnnouncementTypeCategories(unittest.TestCase):
    """Each known category must resolve to its canonical label. Texts are
    synthetic, anonymized announcement snippets (legal-entity debtors only)."""

    CASES = [
        ("Das Insolvenzverfahren über das Vermögen der Muster GmbH wird "
         "eröffnet. Eröffnung des Insolvenzverfahrens am 01.03.2026.", "Eröffnung"),
        ("Es werden Sicherungsmaßnahmen angeordnet. Anordnung der vorläufigen "
         "Insolvenzverwaltung über die Beispiel AG.", "Sicherungsmaßnahmen"),
        ("Anordnung der vorläufigen Insolvenzverwaltung.", "Sicherungsmaßnahmen"),
        ("Es wird Berichtstermin bestimmt auf den 10.04.2026.", "Berichtstermin"),
        ("Es wird Prüfungstermin bestimmt auf den 12.04.2026.", "Prüfungstermin"),
        ("Die Verwertung der Masse wird durchgeführt.", "Verwertung"),
        ("Masseverwertung angezeigt.", "Verwertung"),
        ("Das Verteilungsverzeichnis liegt zur Einsicht aus. Verteilung.", "Verteilung"),
        ("Die Schlussverteilung wird durchgeführt.", "Schlussverteilung"),
        ("Es wird Schlusstermin bestimmt auf den 12.05.2026.", "Schlussverteilung"),
        ("Aufhebung des Insolvenzverfahrens der Muster GmbH.", "Aufhebung"),
        ("Das Verfahren wird mangels Masse eingestellt. Einstellung mangels "
         "Masse.", "Einstellung mangels Masse"),
        ("Der Antrag wird abgewiesen. Abweisung mangels Masse.", "Abweisung mangels Masse"),
        ("Es wird Masseunzulänglichkeit angezeigt.", "Masseunzulänglichkeit"),
        ("Ankündigung der Restschuldbefreiung.", "Restschuldbefreiung"),
        ("Die Vergütung des Insolvenzverwalters wird festgesetzt. "
         "Vergütungsfestsetzung.", "Vergütungsfestsetzung"),
        ("Das Verfahren wird eingestellt.", "Einstellung"),
    ]

    def test_each_category(self):
        for text, expected in self.CASES:
            with self.subTest(expected=expected):
                self.assertEqual(detect_announcement_type(text), expected)


class TestUmlautNormalization(unittest.TestCase):
    """ae/oe/ue/ss spellings must resolve to the same canonical label as the
    umlaut spellings — the core Phase 0037D fix."""

    def test_eroeffnung_folded(self):
        self.assertEqual(
            detect_announcement_type("Eroeffnung des Insolvenzverfahrens."),
            "Eröffnung",
        )

    def test_pruefungstermin_folded(self):
        self.assertEqual(
            detect_announcement_type("Es wird Pruefungstermin bestimmt."),
            "Prüfungstermin",
        )

    def test_sicherungsmassnahme_folded(self):
        self.assertEqual(
            detect_announcement_type("Es werden Sicherungsmassnahmen angeordnet."),
            "Sicherungsmaßnahmen",
        )

    def test_masseunzulaenglichkeit_folded(self):
        self.assertEqual(
            detect_announcement_type("Masseunzulaenglichkeit angezeigt."),
            "Masseunzulänglichkeit",
        )

    def test_umlaut_and_folded_agree(self):
        self.assertEqual(
            detect_announcement_type("Prüfungstermin"),
            detect_announcement_type("Pruefungstermin"),
        )


class TestMissingTypeStaysNull(unittest.TestCase):
    def test_empty_text_is_none(self):
        self.assertIsNone(detect_announcement_type(""))
        self.assertIsNone(detect_announcement_type(None))

    def test_unrecognized_text_is_none(self):
        # No recognizable type keyword -> never fabricated.
        self.assertIsNone(
            detect_announcement_type("Allgemeine Mitteilung ohne Kategorie."))

    def test_full_returns_none_none(self):
        self.assertEqual(detect_announcement_type_full(""), (None, None))


class TestRawValuePreserved(unittest.TestCase):
    def test_raw_keyword_preserved(self):
        hint, raw = detect_announcement_type_full(
            "Eröffnung des Insolvenzverfahrens am 01.03.2026.")
        self.assertEqual(hint, "Eröffnung")
        # The raw (folded) source keyword that matched is kept for auditing.
        self.assertIsNotNone(raw)
        self.assertIn("eroeffnung", raw)

    def test_extract_fields_carries_raw(self):
        fields = extract_fields_from_text("Die Schlussverteilung wird durchgeführt.")
        self.assertEqual(fields["announcement_type_hint"], "Schlussverteilung")
        self.assertEqual(fields["announcement_type_raw"], "schlussverteilung")


class TestContentHashAndExternalRaw(unittest.TestCase):
    def _rec(self, **kw):
        base = dict(
            court="Amtsgericht Musterstadt",
            case_number="000 IN 0000/26",
            announcement_date="2026-03-01",
            announcement_type_hint="Eröffnung",
            announcement_text="Eröffnung des Insolvenzverfahrens der Muster GmbH.",
        )
        base.update(kw)
        return InsolvencyRecord(**base)

    def test_content_hash_is_deterministic_and_stable(self):
        a = self._rec().compute_content_hash()
        b = self._rec().compute_content_hash()
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)  # sha256 hex

    def test_content_hash_available_without_text(self):
        # A row with no detail text still gets a stable id from its proceeding key.
        h = self._rec(announcement_text=None).compute_content_hash()
        self.assertTrue(h)

    def test_to_db_dict_attaches_hash_and_external_raw(self):
        d = self._rec().to_db_dict()
        self.assertIn("content_hash", d)
        self.assertIn("external_raw", d)
        self.assertEqual(d["external_raw"]["announcement_type_hint"], "Eröffnung")
        self.assertEqual(d["external_raw"]["content_hash"], d["content_hash"])

    def test_external_raw_has_no_debtor_identity(self):
        # Privacy: the provenance object must never carry debtor identity.
        raw = self._rec().build_external_raw("deadbeef")
        self.assertNotIn("debtor_name", raw)
        self.assertNotIn("debtor_city", raw)


class TestCoverageMetrics(unittest.TestCase):
    def test_counts(self):
        recs = [
            InsolvencyRecord(announcement_text="Eröffnung.", announcement_type_hint="Eröffnung",
                             source_url="http://x"),
            InsolvencyRecord(announcement_text="Mitteilung ohne Typ.",
                             announcement_type_hint=None, source_url="http://x"),
            InsolvencyRecord(announcement_text=None, announcement_type_hint=None,
                             source_url=None),
        ]
        m = compute_coverage_metrics(recs)
        self.assertEqual(m["total"], 3)
        self.assertEqual(m["with_text"], 2)
        self.assertEqual(m["with_type"], 1)
        self.assertEqual(m["with_source_url"], 2)
        # one text-bearing row lacks a type
        self.assertEqual(m["text_but_no_type"], 1)


if __name__ == "__main__":
    unittest.main()
