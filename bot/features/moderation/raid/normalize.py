"""
bot/features/moderation/raid/normalize.py
=========================================
Text-Normalisierung fuer den fortgeschrittenen Wortfilter.

Der Wortfilter soll nicht an trivialen Umgehungen scheitern.  Deshalb wird
jede Nachricht in mehrere Sichten ("Varianten") zerlegt:

  norm      NFKC + casefold + Zero-Width entfernt + Whitespace geglaettet.
            Behaelt Wortgrenzen -> Basis fuer Wort-Treffer und Regex.
  alpha     norm ohne Nicht-Alphanumerik (Satzzeichen, Symbole, Trenner).
            Basis fuer die Umgehungs-Erkennung ("b.a.d" -> "bad").
  skeleton  alpha zusaetzlich mit Leetspeak-Aufloesung, Homoglyph-Mapping
            (kyrillisch/griechisch -> lateinisch) und Kollaps von
            Zeichenwiederholungen ("bäääd" -> "bad", "b4d" -> "bad").

Damit bleiben die Positionen eines Treffers auf den Originaltext
zurueckfuehrbar (TextMap), was fuer den URL-/Code-Kontextcheck gebraucht wird.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List

# ── Zeichenklassen ──────────────────────────────────────────────────────────

# Zero-Width- und unsichtbare Zeichen, mit denen Woerter zerrissen werden.
INVISIBLE_CHARS = (
    "\u00ad"  # soft hyphen
    "\u034f"  # combining grapheme joiner
    "\u061c"  # arabic letter mark
    "\u115f"  # hangul choseong filler
    "\u1160"  # hangul jungseong filler
    "\u17b4\u17b5"
    "\u180b\u180c\u180d\u180e"
    "\u200b\u200c\u200d\u200e\u200f"
    "\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2061\u2062\u2063\u2064\u2065\u2066\u2067\u2068\u2069\u206a\u206b"
    "\u206c\u206d\u206e\u206f"
    "\u3164"
    "\ufeff\uffa0"
    "\U0001d173\U0001d174\U0001d175\U0001d176\U0001d177\U0001d178\U0001d179"
    "\U0001d17a"
    "\U000e0001\U000e0020\U000e007f"
)

_INVISIBLE_TABLE = {ord(ch): None for ch in INVISIBLE_CHARS}

# Homoglyphe: Zeichen, die optisch wie lateinische Buchstaben aussehen.
# Der Filter laeuft immer auf casefoldetem Text, deshalb genuegen die
# Kleinbuchstaben; die Grossbuchstaben sind als Reserve mit dabei.
HOMOGLYPHS = {
    # Kyrillisch
    "а": "a", "А": "a", "в": "b", "В": "b", "с": "c", "С": "c", "е": "e",
    "Е": "e", "ё": "e", "Ё": "e", "н": "h", "Н": "h", "і": "i", "І": "i",
    "ї": "i", "Ї": "i", "ј": "j", "Ј": "j", "к": "k", "К": "k", "м": "m",
    "М": "m", "о": "o", "О": "o", "р": "p", "Р": "p", "ѕ": "s", "Ѕ": "s",
    "т": "t", "Т": "t", "у": "y", "У": "y", "х": "x", "Х": "x", "ԁ": "d",
    "Ԍ": "g", "ԛ": "q", "ԝ": "w", "ѡ": "w", "ѵ": "v", "Ѵ": "v", "һ": "h",
    "Һ": "h", "ӏ": "l", "Ӏ": "l", "г": "r", "Г": "r", "п": "n", "П": "n",
    "и": "u", "И": "u", "ѐ": "e", "ѝ": "i",
    # Griechisch
    "α": "a", "Α": "a", "β": "b", "Β": "b", "ϲ": "c", "Ϲ": "c", "ε": "e",
    "Ε": "e", "ι": "i", "Ι": "i", "κ": "k", "Κ": "k", "μ": "u", "Μ": "m",
    "ν": "v", "Ν": "n", "ο": "o", "Ο": "o", "ρ": "p", "Ρ": "p", "τ": "t",
    "Τ": "t", "υ": "u", "Υ": "y", "χ": "x", "Χ": "x", "ϳ": "j",
    # Sonderbuchstaben, die NFKC nicht aufloest
    "ℓ": "l", "ⅰ": "i", "ⅴ": "v", "ⅹ": "x", "ⓐ": "a", "ſ": "s", "ƅ": "b",
    "ɡ": "g", "ɩ": "i", "ɪ": "i", "ʟ": "l", "ᴏ": "o", "ᴜ": "u", "ᴠ": "v",
}

# Leetspeak: Ziffern/Symbole -> Buchstabe.  ACHTUNG: Diese Tabelle wird
# bewusst NICHT auf Ziffern angewendet (siehe `decode_char`), damit eine
# verbotene Zahl exakt bleibt und "77" nicht zu "tt" wird.
LEET_TABLE = {
    "0": "o",
    "1": "i",
    "2": "z",
    "3": "e",
    "4": "a",
    "5": "s",
    "6": "g",
    "7": "t",
    "8": "b",
    "9": "g",
    "!": "i",
    "|": "l",
    "$": "s",
    "@": "a",
    "+": "t",
    "£": "l",
    "€": "e",
}

# Fuer Zeichen, fuer die es eine eindeutige Alternative gibt ("£" -> "l").
_HOMOGLYPH_TRANSLATION = str.maketrans(HOMOGLYPHS)
_LEET_TRANSLATION = str.maketrans(LEET_TABLE)


def decode_char(char: str) -> str:
    """
    Loest ein Zeichen in seine "normale" Schreibweise auf: Homoglyphe und
    Leetspeak ("4" -> "a", "b4d" -> "bad").

    Ziffern-zu-Buchstabe gilt nur fuer Ziffern, die nicht schon als Ziffer
    eindeutig sind - deshalb prueft der Matcher Zahlen zusaetzlich exakt
    (`_token_matches`), damit "77" niemals auf "7" passt.
    """
    mapped = _HOMOGLYPH_TRANSLATION.get(ord(char))
    if mapped is not None:
        return mapped
    return LEET_TABLE.get(char, char)


# Zeichen, die als Trenner im Inneren eines Wortes gelten. Sie werden fuer
# die Wortgrenzenpruefung NICHT als Teil des Wortes betrachtet.
SEPARATOR_CHARS = set("._|/\\-*~^=+:,;!?\"'`´’‚“”„«»<>()[]{}")

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


# ── Basis-Normalisierung ────────────────────────────────────────────────────

def _nfkc_lower(text: str) -> str:
    """NFKC + casefold. Beides ist (fuer die hier genutzten Zeichen)
    laengenstabil, damit Zeichenpositionen erhalten bleiben."""
    if not text:
        return ""
    return unicodedata.normalize("NFKC", text).casefold()


def normalize_reference(value: str) -> str:
    """
    Normalisiert ein Filter-Wort oder einen Whitelist-Eintrag exakt so, wie
    eine eingehende Nachricht normalisiert wird (ohne Whitespace/Invisible).

    Wird beim Anlegen von Eintraegen benutzt, um Duplikate zu erkennen und die
    Eingabe des Admins mit dem spaeteren Treffer zu vergleichen.
    """
    text = _nfkc_lower(value or "")
    text = text.translate(_INVISIBLE_TABLE)
    text = _WHITESPACE_RE.sub("", text)
    return text.strip()


def _collapse_repeats(text: str) -> str:
    out: List[str] = []
    previous = None
    for char in text:
        if char != previous:
            out.append(char)
        previous = char
    return "".join(out)


# ── Zeichen-Mapping Original -> Variante ────────────────────────────────────

@dataclass(frozen=True)
class TextMap:
    """Eine Textvariante zusammen mit der Rueckabbildung auf `norm`."""

    text: str
    # maps[i] = Index des Zeichens in `norm`, das text[i] erzeugt hat.
    maps: tuple[int, ...]

    def span_to_norm(self, start: int, end: int) -> tuple[int, int]:
        """Uebersetzt einen Trefferbereich in `norm`-Koordinaten."""
        if not self.maps or start >= len(self.maps):
            return (0, 0)
        norm_start = self.maps[max(0, start)]
        last = min(max(end - 1, 0), len(self.maps) - 1)
        return (norm_start, self.maps[last] + 1)


@dataclass(frozen=True)
class TextVariants:
    norm: str
    alpha: TextMap
    skeleton: TextMap


def _alpha_map(norm: str) -> TextMap:
    """Entfernt alles ausser Buchstaben/Ziffern und merkt die Herkunft."""
    chars: List[str] = []
    maps: List[int] = []
    for index, char in enumerate(norm):
        if char.isspace() or not char.isalnum():
            continue
        chars.append(char)
        maps.append(index)
    return TextMap("".join(chars), tuple(maps))


def _skeleton_map(norm: str) -> TextMap:
    """
    Erzeugt die aggressivste Variante: Trenner weg, Homoglyphe/Leet aufloesen,
    Zeichenwiederholungen zusammenfassen.  Bewusst auf dem Bereinigungs-Text
    aufgebaut, damit die Rueckabbildung eindeutig bleibt.

    Wichtig: Wiederholungen werden nur bei Buchstaben zusammengefasst.
    Ziffern muessen exakt bleiben, sonst wuerde "77" zu "7" und eine
    verbotene "7" traefe mitten in "77".
    """
    raw = "".join(char for char in norm if not char.isspace() and char.isalnum())
    positions = [index for index, char in enumerate(norm) if not char.isspace() and char.isalnum()]

    chars: List[str] = []
    maps: List[int] = []
    previous = None
    for char, position in zip(raw, positions):
        mapped = decode_char(char)
        for out_char in mapped:
            if out_char == previous and not out_char.isdigit():
                continue
            chars.append(out_char)
            maps.append(position)
            previous = out_char
    return TextMap("".join(chars), tuple(maps))


def text_variants(text: str) -> TextVariants:
    norm = _nfkc_lower(text or "")
    # Invisible-Zeichen verschieben keine Positionen, weil sie geloescht
    # werden; `translate` mit None-Werten entfernt sie vollstaendig.
    norm = norm.translate(_INVISIBLE_TABLE)
    norm = norm.replace("\u00a0", " ").replace("\u2028", " ").replace("\u2029", " ")
    norm = _WHITESPACE_RE.sub(" ", norm).strip()
    return TextVariants(norm=norm, alpha=_alpha_map(norm), skeleton=_skeleton_map(norm))


# ── Tokens ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int


def tokenize(norm: str) -> List[Token]:
    """
    Zerlegt den normalisierten Text in Woerter.

    `re`\\w+ ist Unicode-bewusst, also ist "123" ein Token und "1234" ein
    anderes.  Genau das brauchen wir fuer die Zahlenregel: eine verbotene Zahl
    trifft nur, wenn sie als eigenstaendige Zahl vorkommt.
    """
    return [Token(match.group(0), match.start(), match.end()) for match in _TOKEN_RE.finditer(norm)]


def is_numeric(text: str) -> bool:
    """True, wenn der Text ausschliesslich aus Ziffern besteht."""
    return bool(text) and all(char.isdigit() for char in text)


def is_ascii(text: str) -> bool:
    return all(ord(char) < 128 for char in text)
