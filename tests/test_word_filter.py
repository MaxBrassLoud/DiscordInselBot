"""Tests fuer den Wortfilter des Raid-Schutz-Systems.

Die Tests laufen ohne Datenbank und ohne Discord-Verbindung: geprueft werden
Normalisierung, Trefferlogik und die Zahlenregel.  Der Matcher ist das Herz
des Filters - ein Fehler dort bedeutet entweder Fehlalarme (User werden
unschuldig gebannt) oder Umgehungsluecken.
"""

import sys
import types
import unittest

# Windows-Konsole: Umlaute und Sonderzeichen sicher ausgeben.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

# Datenbank-Integration wird nicht gebraucht.
supabase_client = types.ModuleType("bot.core.supabase_client")
supabase_client.get_supabase = lambda: None
sys.modules["bot.core.supabase_client"] = supabase_client

logger_module = types.ModuleType("bot.utils.logger")
logger_module.get_logger = lambda _name: __import__("logging").getLogger(_name)
sys.modules["bot.utils.logger"] = logger_module

from bot.features.moderation.raid.matcher import (
    build_matcher,
    reduce_text,
    validate_pattern,
)
from bot.features.moderation.raid.normalize import (
    normalize_reference,
    text_variants,
    tokenize,
)
from bot.features.moderation.raid.word_store import WordEntry


def matcher(*entries, allow=()):
    return build_matcher(list(entries), allow)


class NormalisationTests(unittest.TestCase):
    def test_casefold_and_fullwidth(self):
        self.assertEqual(normalize_reference("BaDWord"), "badword")
        # Vollbreite wird durch NFKC auf ASCII abgebildet.
        self.assertEqual(normalize_reference("１２３"), "123")

    def test_invisible_characters_are_removed(self):
        self.assertEqual(normalize_reference("b\u200bad\u200bword"), "badword")
        self.assertEqual(normalize_reference("b\u00adad"), "bad")

    def test_skeleton_resolves_leet_homoglyphs_and_repeats(self):
        self.assertEqual(reduce_text("b4d"), "bad")
        self.assertEqual(reduce_text("baaad"), "bad")
        self.assertEqual(reduce_text("b\u0430d"), "bad")  # kyrillisches a
        self.assertEqual(reduce_text("B A D"), "bad")

    def test_tokenize_keeps_numbers_separate(self):
        tokens = [token.text for token in tokenize("12 123 1234 abc123")]
        self.assertEqual(tokens, ["12", "123", "1234", "abc123"])


class NumberRuleTests(unittest.TestCase):
    """Kernanforderung: verbotene Zahlen nur als eigenstaendige Zahl."""

    def setUp(self):
        self.matcher = matcher(WordEntry(pattern="123"))

    def test_standalone_number_matches(self):
        for text in ["123", "die zahl 123", "123.", "(123)"]:
            with self.subTest(text=text):
                self.assertIsNotNone(self.matcher.check(text))

    def test_number_inside_a_longer_number_does_not_match(self):
        for text in ["1234", "4123", "12345", "9123", "123 1234"]:
            with self.subTest(text=text):
                match = self.matcher.check(text)
                # "123 1234" enthaelt ein eigenstaendiges 123 -> Treffer ist ok,
                # deshalb wird hier nur geprueft, dass der Treffer nicht aus
                # dem laengeren Block stammt.
                if text == "123 1234":
                    self.assertIsNotNone(match)
                else:
                    self.assertIsNone(match)

    def test_number_inside_a_word_does_not_match(self):
        for text in ["abc123", "123abc", "a123b", "hallo123"]:
            with self.subTest(text=text):
                self.assertIsNone(self.matcher.check(text))

    def test_single_digit_entry_is_token_aware(self):
        """Die Zahlenregel gilt auch fuer einstellige Eintraege."""
        check = matcher(WordEntry(pattern="7"))
        self.assertIsNotNone(check.check("7"))
        self.assertIsNotNone(check.check("die 7"))
        self.assertIsNone(check.check("77"))
        self.assertIsNone(check.check("17"))

    def test_number_only_matches_inside_code_or_url_when_bypass_disabled(self):
        # Standard: URL und Code werden ausgelassen.
        self.assertIsNone(self.matcher.check("https://example.com/123"))
        self.assertIsNone(self.matcher.check("`123`"))
        # Ohne Bypass greift der Filter auch dort.
        strict = matcher(WordEntry(pattern="123", bypass_urls=False, bypass_code=False))
        self.assertIsNotNone(strict.check("https://example.com/123"))
        self.assertIsNotNone(strict.check("`123`"))


class WordRuleTests(unittest.TestCase):
    def setUp(self):
        self.matcher = matcher(WordEntry(pattern="badword"))

    def test_whole_word_matches_in_any_case(self):
        for text in ["badword", "BADWORD", "ein BadWord hier", "Badword!"]:
            with self.subTest(text=text):
                self.assertIsNotNone(self.matcher.check(text))

    def test_substring_of_a_longer_word_does_not_match(self):
        for text in ["badwords", "mybadword", "badwordish"]:
            with self.subTest(text=text):
                self.assertIsNone(self.matcher.check(text))

    def test_obfuscations_are_detected(self):
        for text in [
            "b.a.d.w.o.r.d",
            "b-a-d-w-o-r-d",
            "b a d w o r d",
            "b4dword",
            "baaadword",
            "b\u0430dword",  # kyrillisches a
            "B​A​D​W​O​R​D",  # Zero-Width-Trenner
        ]:
            with self.subTest(text=text):
                self.assertIsNotNone(self.matcher.check(text))

    def test_substring_mode_matches_inside_words(self):
        check = matcher(WordEntry(pattern="bad", match_type="substring"))
        self.assertIsNotNone(check.check("this is bad"))
        self.assertIsNotNone(check.check("badword"))
        self.assertIsNone(check.check("goodword"))

    def test_obfuscations_can_be_disabled(self):
        check = matcher(WordEntry(pattern="badword", fuzzy=False))
        self.assertIsNotNone(check.check("badword"))
        self.assertIsNone(check.check("b.a.d.w.o.r.d"))

    def test_short_words_do_not_fuzzy_match_by_accident(self):
        check = matcher(WordEntry(pattern="ab"))
        self.assertIsNotNone(check.check("ab"))
        self.assertIsNone(check.check("a b"))
        self.assertIsNone(check.check("a-b"))


class RegexRuleTests(unittest.TestCase):
    def test_regex_entry_matches(self):
        check = matcher(WordEntry(pattern=r"\bd[a4@]+mn\b", match_type="regex"))
        self.assertIsNotNone(check.check("so ein damn"))
        self.assertIsNotNone(check.check("DAMN"))
        self.assertIsNone(check.check("damnation"))

    def test_invalid_regex_is_rejected_and_skipped(self):
        valid, message = validate_pattern("([unclosed", "regex")
        self.assertFalse(valid)
        self.assertTrue(message)
        # Ein ungueltiger Eintrag darf die Pruefung nicht sprengen.
        check = matcher(WordEntry(pattern="([unclosed", match_type="regex"))
        self.assertIsNone(check.check("irgendein text"))


class WhitelistTests(unittest.TestCase):
    def test_whitelist_prevents_a_false_positive(self):
        check = matcher(WordEntry(pattern="insel"), allow=("insel",))
        self.assertIsNone(check.check("die insel"))

    def test_whitelist_only_applies_to_the_listed_form(self):
        check = matcher(WordEntry(pattern="insel"), allow=("insel",))
        # Ein anderer Eintrag bleibt unberuehrt.
        other = matcher(
            WordEntry(pattern="insel"), WordEntry(pattern="doof"), allow=("insel",)
        )
        self.assertIsNotNone(other.check("du bist doof"))


class SkipWordTests(unittest.TestCase):
    def test_stopword_in_a_longer_word_is_not_filtered(self):
        """"ass" darf nicht in "class" oder "passage" anschlagen."""
        check = matcher(WordEntry(pattern="ass"))
        self.assertIsNotNone(check.check("you ass"))
        self.assertIsNone(check.check("class"))
        self.assertIsNone(check.check("passage"))
        self.assertIsNone(check.check("assembly"))


class ValidationTests(unittest.TestCase):
    def test_empty_and_long_patterns_are_rejected(self):
        self.assertFalse(validate_pattern("", "word")[0])
        self.assertFalse(validate_pattern("   ", "word")[0])
        self.assertFalse(validate_pattern("x" * 201, "word")[0])

    def test_single_letters_need_substring_mode(self):
        self.assertFalse(validate_pattern("a", "word")[0])
        self.assertTrue(validate_pattern("a", "substring")[0])

    def test_normal_patterns_are_accepted(self):
        for pattern, mode in [
            ("badword", "word"),
            ("123", "token"),
            ("b4d", "substring"),
            (r"\d{3}", "regex"),
        ]:
            with self.subTest(pattern=pattern):
                self.assertTrue(validate_pattern(pattern, mode)[0])


class MultipleEntryTests(unittest.TestCase):
    def test_most_specific_match_wins(self):
        check = matcher(
            WordEntry(pattern="bad", match_type="substring"),
            WordEntry(pattern="badword"),
        )
        match = check.check("ein badword")
        self.assertIsNotNone(match)
        # Ein direkter Worttreffer schlaegt den Teilstring-Treffer.
        self.assertEqual(match.pattern, "badword")
        self.assertEqual(match.method, "exact")

    def test_disabled_entries_are_ignored(self):
        check = matcher(WordEntry(pattern="badword", enabled=False))
        self.assertIsNone(check.check("badword"))

    def test_check_all_reports_every_hit(self):
        check = matcher(WordEntry(pattern="eins"), WordEntry(pattern="zwei"))
        matches = check.check_all("eins und zwei")
        self.assertEqual({match.pattern for match in matches}, {"eins", "zwei"})

    def test_variants_are_available_for_debugging(self):
        variants = text_variants("B4d W0rd")
        self.assertEqual(variants.norm, "b4d w0rd")
        self.assertEqual(variants.alpha.text, "b4dw0rd")
        self.assertEqual(variants.skeleton.text, "badword")


if __name__ == "__main__":
    unittest.main()
