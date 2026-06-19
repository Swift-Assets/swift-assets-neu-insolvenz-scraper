"""Focused unit tests for the phase classifier and the stable unique key.

Run with:  python -m unittest discover -s tests
(stdlib unittest only — no extra test dependency required.)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import (  # noqa: E402
    InsolvencyRecord,
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


if __name__ == "__main__":
    unittest.main()
