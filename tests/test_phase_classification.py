"""Focused unit tests for the phase classifier and the stable unique key.

Run with:  python -m unittest discover -s tests
(stdlib unittest only — no extra test dependency required.)
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper  # noqa: E402
from scraper import (  # noqa: E402
    InsolvencyRecord,
    SupabaseClient,
    classify_phase,
    PHASE_UNKNOWN,
)


class TestClassifyPhase(unittest.TestCase):
    def test_opening(self):
        text = "Das Insolvenzverfahren wird eröffnet. Eröffnung des Insolvenzverfahrens am 01.03.2026."
        phase, pre = classify_phase(text)
        self.assertEqual(phase, "opening")
        self.assertTrue(pre)

    def test_preliminary_administration(self):
        text = "Anordnung der vorläufigen Insolvenzverwaltung. Zum vorläufigen Insolvenzverwalter wird RA Müller bestellt."
        phase, pre = classify_phase(text)
        self.assertEqual(phase, "preliminary_administration")
        self.assertTrue(pre)

    def test_sicherungsmassnahmen(self):
        phase, pre = classify_phase("Es werden Sicherungsmaßnahmen angeordnet.")
        self.assertEqual(phase, "preliminary_administration")
        self.assertTrue(pre)

    def test_administrator_appointed(self):
        text = "Zur Insolvenzverwalterin wird Frau Dr. Schmidt bestellt."
        phase, pre = classify_phase(text)
        self.assertEqual(phase, "administrator_appointed")
        self.assertTrue(pre)

    def test_schlusstermin_is_late(self):
        phase, pre = classify_phase("Es wird Schlusstermin bestimmt auf den 12.05.2026.")
        self.assertEqual(phase, "schlusstermin")
        self.assertFalse(pre)

    def test_schlussverteilung_is_late(self):
        phase, pre = classify_phase("Die Schlussverteilung wird durchgeführt.")
        self.assertEqual(phase, "schlussverteilung")
        self.assertFalse(pre)

    def test_aufhebung_is_late(self):
        phase, pre = classify_phase("Aufhebung des Insolvenzverfahrens.")
        self.assertEqual(phase, "aufhebung")
        self.assertFalse(pre)

    def test_einstellung_mangels_masse(self):
        phase, pre = classify_phase("Das Verfahren wird mangels Masse eingestellt. Einstellung mangels Masse.")
        self.assertEqual(phase, "dismissed_lack_of_assets")
        self.assertFalse(pre)

    def test_abweisung_mangels_masse(self):
        phase, pre = classify_phase("Der Antrag wird abgewiesen. Abweisung mangels Masse.")
        self.assertEqual(phase, "dismissed_lack_of_assets")
        self.assertFalse(pre)

    def test_masseunzulaenglichkeit(self):
        phase, pre = classify_phase("Es wird Masseunzulänglichkeit angezeigt.")
        self.assertEqual(phase, "masseunzulaenglichkeit")
        self.assertFalse(pre)

    def test_restschuldbefreiung_is_late(self):
        phase, pre = classify_phase("Ankündigung der Restschuldbefreiung.")
        self.assertEqual(phase, "restschuldbefreiung")
        self.assertFalse(pre)

    def test_late_signal_wins_over_opening(self):
        # An announcement that references both the original opening and a
        # closing event must be classified as late-stage (not acquirable).
        text = (
            "Nach Eröffnung des Insolvenzverfahrens am 01.01.2025 wird nun "
            "die Schlussverteilung durchgeführt."
        )
        phase, pre = classify_phase(text)
        self.assertEqual(phase, "schlussverteilung")
        self.assertFalse(pre)

    def test_umlaut_folding(self):
        # ae/oe/ue/ss spelled-out variants must still match.
        phase, pre = classify_phase("Eroeffnung des Insolvenzverfahrens.")
        self.assertEqual(phase, "opening")
        self.assertTrue(pre)

    def test_whitespace_tolerance(self):
        phase, pre = classify_phase("Eröffnung   des\n\n  Insolvenzverfahrens")
        self.assertEqual(phase, "opening")
        self.assertTrue(pre)

    def test_ambiguous_is_unknown(self):
        phase, pre = classify_phase("Eine allgemeine Bekanntmachung ohne klare Signale.")
        self.assertEqual(phase, PHASE_UNKNOWN)
        self.assertIsNone(pre)

    def test_empty(self):
        self.assertEqual(classify_phase(""), (PHASE_UNKNOWN, None))
        self.assertEqual(classify_phase(None), (PHASE_UNKNOWN, None))


class TestStableUniqueKey(unittest.TestCase):
    def _rec(self, **kw) -> InsolvencyRecord:
        base = dict(
            court="Amtsgericht Berlin",
            case_number="36a IN 1234/25",
            announcement_date="2026-03-01",
            registry_court="Berlin (Charlottenburg)",
            registry_type="HRB",
            registry_number="12345",
        )
        base.update(kw)
        return InsolvencyRecord(**base)

    def test_key_is_six_stable_fields(self):
        rec = self._rec()
        self.assertEqual(
            rec.unique_key(),
            (
                "Amtsgericht Berlin", "36a IN 1234/25", "2026-03-01",
                "Berlin (Charlottenburg)", "HRB", "12345",
            ),
        )

    def test_key_stable_across_type_hint_change(self):
        # The bug: the same announcement hashed differently before vs after
        # classification because announcement_type_hint was part of the key.
        before = self._rec(announcement_type_hint=None)
        after = self._rec(announcement_type_hint="Eröffnung")
        self.assertEqual(before.unique_key(), after.unique_key())

    def test_key_stable_across_phase_change(self):
        before = self._rec(insolvency_phase=None, is_pre_verteilung=None)
        after = self._rec(insolvency_phase="opening", is_pre_verteilung=True)
        self.assertEqual(before.unique_key(), after.unique_key())

    def test_key_matches_stable_key_from_row(self):
        rec = self._rec()
        row = {
            "court": "Amtsgericht Berlin",
            "case_number": "36a IN 1234/25",
            "announcement_date": "2026-03-01",
            "registry_court": "Berlin (Charlottenburg)",
            "registry_type": "HRB",
            "registry_number": "12345",
        }
        self.assertEqual(rec.unique_key(), SupabaseClient._stable_key_from_row(row))

    def test_key_normalizes_empty_string_and_whitespace(self):
        # Regression for M2: a DB value stored as '' or with stray whitespace
        # must match a scraper-side None / trimmed value, or dedup silently
        # breaks (the same row looks both "new" and "existing").
        rec = InsolvencyRecord(
            court="Amtsgericht Berlin",
            case_number="36a IN 1234/25",
            announcement_date="2026-03-01",
            registry_court=None,        # scraper couldn't parse a register
            registry_type=None,
            registry_number=None,
        )
        row = {
            "court": "  Amtsgericht Berlin ",   # stray whitespace in DB
            "case_number": "36a IN 1234/25",
            "announcement_date": "2026-03-01",
            "registry_court": "",                # stored as empty string in DB
            "registry_type": "",
            "registry_number": "",
        }
        self.assertEqual(rec.unique_key(), SupabaseClient._stable_key_from_row(row))


class TestToDbDictPhaseFields(unittest.TestCase):
    def _rec(self, **kw) -> InsolvencyRecord:
        base = dict(court="AG Berlin", case_number="1 IN 1/25",
                    announcement_date="2026-03-01")
        base.update(kw)
        return InsolvencyRecord(**base)

    def _with_write_phase(self, value: bool):
        # to_db_dict reads the module-level WRITE_PHASE_FIELDS at call time.
        self.addCleanup(setattr, scraper, "WRITE_PHASE_FIELDS",
                        scraper.WRITE_PHASE_FIELDS)
        scraper.WRITE_PHASE_FIELDS = value

    def test_is_pre_verteilung_serializes_as_native_bool(self):
        self._with_write_phase(True)
        d = self._rec(insolvency_phase="opening", is_pre_verteilung=True).to_db_dict()
        self.assertIn("is_pre_verteilung", d)
        self.assertIs(d["is_pre_verteilung"], True)
        # And survives JSON encoding as a JSON boolean, not a string.
        self.assertIn('"is_pre_verteilung": true', json.dumps(d))

    def test_is_pre_verteilung_false_is_kept(self):
        self._with_write_phase(True)
        d = self._rec(insolvency_phase="aufhebung", is_pre_verteilung=False).to_db_dict()
        self.assertIs(d["is_pre_verteilung"], False)
        self.assertIn('"is_pre_verteilung": false', json.dumps(d))

    def test_is_pre_verteilung_none_is_omitted(self):
        self._with_write_phase(True)
        d = self._rec(insolvency_phase="opening", is_pre_verteilung=None).to_db_dict()
        self.assertNotIn("is_pre_verteilung", d)

    def test_unknown_phase_is_omitted(self):
        # Regression for M1: never send the "unknown" placeholder.
        self._with_write_phase(True)
        d = self._rec(insolvency_phase=PHASE_UNKNOWN, is_pre_verteilung=None).to_db_dict()
        self.assertNotIn("insolvency_phase", d)

    def test_real_phase_is_included(self):
        self._with_write_phase(True)
        d = self._rec(insolvency_phase="opening", is_pre_verteilung=True).to_db_dict()
        self.assertEqual(d["insolvency_phase"], "opening")

    def test_phase_fields_omitted_when_disabled(self):
        self._with_write_phase(False)
        d = self._rec(insolvency_phase="opening", is_pre_verteilung=True).to_db_dict()
        self.assertNotIn("insolvency_phase", d)
        self.assertNotIn("is_pre_verteilung", d)


if __name__ == "__main__":
    unittest.main()
