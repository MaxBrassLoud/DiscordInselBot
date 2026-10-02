"""
bot/features/moderation/raid/matcher.py
=======================================
Der eigentliche Wortfilter-Algorithmus.

Design-Entscheidungen
---------------------
* **Zahlen sind eigenstaendige Tokens.**  Ist "123" verboten, dann trifft
  "123" und " 123 " - aber *nicht* "1234", "4123" oder "abc123".  Ein
  Treffer entsteht nur, wenn das komplette Token (bzw. der komplette
  Ziffernblock) der Zahl entspricht.
* **Vier Trefferarten** (pro Eintrag waehlbar):
    word       ganzes Wort (Standard)
    token      ganzes Token, Zahlen immer als ganzer Ziffernblock
    substring  irgendwo im Text (bewusst unscharf, z. B. fuer Wortstaemme)
    regex      vollstaendiger regulaerer Ausdruck
* **Umgehungs-Erkennung.**  Zusaetzlich wird eine "Skeleton"-Sicht geprueft:
  Leetspeak (b4d -> bad), Homoglyphe (kyrillisches а -> a), again
  Zeichenwiederholungen (baaad -> bad) und eingestreute Trenner (b.a.d -> bad).
* **Whitelist.**  Ein Whitelist-Eintrag (globale Woerter oder die konkrete
  Tokenform) verhindert einen Treffer und damit Fehlalarme.
* **Kontext.**  Treffer in URLs und in Code-/Mention-Kontext werden ignoriert
  (pro Eintrag abschaltbar).
* **Robustheit.**  Ungueltige Regex-Eintraege werden uebersprungen und nur
  einmal geloggt; Regex-Laeufe haben ein Zeitlimit, damit ein boesartiger
  Ausdruck den Bot nicht blockiert.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from bot.utils.logger import get_logger

from .normalize import (
    TextVariants,
    is_ascii,
    is_numeric,
    normalize_reference,
    text_variants,
    tokenize,
)
from .word_store import WordEntry

logger = get_logger("raid.wordfilter")

# Zeitlimit fuer einen einzelnen Regex-Lauf (Schutz gegen ReDoS).
try:  # pragma: no cover - nur auf sehr alten Python-Versionen relevant
    from re import TIMEOUT  # type: ignore
except ImportError:  # pragma: no cover
    TIMEOUT = None

REGEX_TIMEOUT_SECONDS = 0.1
MAX_PATTERN_LENGTH = 200

BYPASS_FLAGS = re.IGNORECASE | re.UNICODE

# Zeichen, die einen Code-/Sonderzeichen-Kontext anzeigen.  Satzzeichen wie
# "." oder "-" fehlen hier bewusst: sie werden als Trenner in Woertern
# benutzt ("b.a.d") und wuerden sonst jede Umgehung blockieren.
_CODE_CHARS = set("`´_/\\|<>^*~")

_invalid_patterns_logged: set[str] = set()
_invalid_patterns: set[str] = set()
_invalid_lock_flag = False


# ── Ergebnisse ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Collected:
    """Alphanumerische Sicht eines Textes samt Herkunftspositionen."""

    text: str
    positions: Tuple[int, ...]


@dataclass(frozen=True)
class WordMatch:
    entry: WordEntry
    method: str          # exact | obfuscated | regex | substring
    reason: str          # menschenlesbare Begruendung
    confidence: float    # 0.0 - 1.0
    span: Tuple[int, int]  # Bereich im normalisierten Text
    matched_text: str

    @property
    def pattern(self) -> str:
        return self.entry.pattern

    @property
    def score(self) -> float:
        return self.confidence


# ── Pattern-Aufbereitung ────────────────────────────────────────────────────

def _valid_regex(pattern: str) -> bool:
    try:
        re.compile(pattern, BYPASS_FLAGS)
        return True
    except re.error:
        return False


def validate_pattern(pattern: str, match_type: str = "word") -> Tuple[bool, str]:
    """
    Prueft einen Eintrag so, wie er spaeter auch geprueft wird.
    Rueckgabe: (gueltig, Fehlermeldung)
    """
    raw = (pattern or "").strip()
    if not raw:
        return False, "Das Muster ist leer."
    if len(raw) > MAX_PATTERN_LENGTH:
        return (
            False,
            f"Das Muster ist zu lang (max. {MAX_PATTERN_LENGTH} Zeichen).",
        )
    if match_type == "regex":
        if not _valid_regex(raw):
            return False, "Das ist kein gueltiger regulaerer Ausdruck."
        return True, ""

    normalized = normalize_reference(raw)
    if not normalized:
        return False, "Das Muster enthaelt keine Buchstaben oder Ziffern."
    if match_type in ("word", "token") and len(normalized) == 1 and not is_numeric(normalized):
        return (
            False,
            "Einzelne Buchstaben sind nur mit der Trefferart `substring` sinnvoll.",
        )
    return True, ""


# ── Trefferpruefung ─────────────────────────────────────────────────────────

def _reduce_forms(text: str) -> Tuple[str, ...]:
    """
    (alphanumerische Form, Skeleton-Form) - Basis fuer den Whitelist-Vergleich.

    Bei Ziffern wird die Skeleton-Form bewusst ausgelassen, damit ein
    Whitelist-Eintrag "77" nicht den Eintrag "7" freischaltet.
    """
    variants = text_variants(text)
    alpha = variants.alpha.text
    if not alpha:
        return ()
    if alpha.isdigit():
        return (alpha,)
    skeleton = variants.skeleton.text
    if skeleton and skeleton != alpha:
        return (alpha, skeleton)
    return (alpha,)


def _candidate_forms(entry: WordEntry) -> Tuple[str, ...]:
    if entry.keywords:
        return entry.keywords
    forms = [entry.normalized]
    skeleton = text_variants(entry.normalized).skeleton.text
    if skeleton and skeleton not in forms:
        forms.append(skeleton)
    return tuple(forms)


def _token_matches(token: str, candidates: Sequence[str]) -> bool:
    """
    Vergleicht ein Token mit den Kandidatenformen eines Eintrags.

      * Fuer Ziffern-Kandidaten gilt ausschliesslich der exakte Vergleich.
        Das ist die Zahlenregel: eine verbotene "7" trifft nicht "77".
      * Sonst werden alphanumerische Form und Skeleton-Form verglichen, damit
        sowohl "b4d" -> Eintrag "bad" als auch Eintrag "b4d" -> Text "bad"
        funktioniert.
    """
    variants = text_variants(token)
    alpha = variants.alpha.text or token
    if alpha in candidates:
        return True
    if alpha.isdigit():
        return False
    skeleton = variants.skeleton.text
    if not skeleton:
        return False
    return any(
        text_variants(candidate).skeleton.text == skeleton for candidate in candidates
    )


def _is_within_url(norm: str, start: int, end: int) -> bool:
    """True, wenn der Bereich mitten in einer URL steht."""
    for prefix in ("http://", "https://", "www.", "discord.gg/", "cdn.discordapp.com"):
        index = norm.rfind(prefix)
        while index != -1:
            if index < start and end <= index + len(prefix) + 200:
                between = norm[index:start]
                if not any(char.isspace() for char in between):
                    return True
            index = norm.rfind(prefix, 0, index)
    return False


def _is_within_code(norm: str, start: int, end: int) -> bool:
    """True, wenn der Bereich in Code/Backticks/Klammern eingebettet ist."""
    left = norm[max(0, start - 3):start]
    right = norm[end:end + 3]
    if "`" in left or "`" in right:
        return True
    left_char = norm[start - 1] if start > 0 else ""
    right_char = norm[end] if end < len(norm) else ""
    if left_char in _CODE_CHARS or right_char in _CODE_CHARS:
        return True
    return False


def _digit_neighbors(norm: str, start: int, end: int) -> bool:
    """
    True, wenn direkt neben dem Treffer eine Ziffer steht.
    Damit trifft "123" nicht in "4123" oder "1234" - auch dann nicht, wenn
    dazwischen nur ein Trenner steht ("4123" vs "123").
    """
    left = norm[start - 1] if start > 0 else ""
    right = norm[end] if end < len(norm) else ""
    return left.isdigit() or right.isdigit()


def _alpha_token(norm: str, start: int, end: int) -> str:
    return "".join(char for char in norm[start:end] if char.isalnum())


def _is_whitelisted(
    entry: WordEntry, token: str, alpha: str, norm: Optional[str] = None,
    start: Optional[int] = None, end: Optional[int] = None,
) -> bool:
    """
    Prueft, ob ein Treffer durch die Whitelist ausgenommen ist.

    Bei einem Treffer mitten in einem laengeren Wort (Teilstring-Modus) wuerde
    der komplette Token als Kandidat geprueft.  Deshalb wird die Whitelist nur
    angewandt, wenn der Treffer exakt auf Wortgrenzen liegt - sonst koennte
    ein Eintrag in einem groesseren Wort faelschlich durchgewinkt werden.
    """
    if not entry.whitelist:
        return False
    if norm is not None and start is not None and end is not None:
        left_ok = start == 0 or not norm[start - 1].isalnum()
        right_ok = end >= len(norm) or not norm[end].isalnum()
        if not (left_ok and right_ok):
            return False
    candidates = {token, alpha}
    if token:
        candidates.update(_reduce_forms(token))
    if alpha:
        candidates.update(_reduce_forms(alpha))
    return bool({item for item in candidates if item} & set(entry.whitelist))


# ── Matcher ─────────────────────────────────────────────────────────────────

@dataclass
class CompiledWord:
    entry: WordEntry
    obfuscation_regex: Optional[re.Pattern] = None


@dataclass
class WordMatcher:
    """Prueft Text gegen eine Liste von Filter-Eintraegen."""

    entries: List[WordEntry] = field(default_factory=list)
    _compiled: List[CompiledWord] = field(default_factory=list, repr=False)

    # -- Aufbau --
    def compile(self) -> None:
        compiled: List[CompiledWord] = []
        seen: set[Tuple[str, str]] = set()
        for entry in self.entries:
            if not entry.enabled:
                continue
            key = (entry.pattern, entry.effective_match_type)
            if key in seen:
                continue
            seen.add(key)
            compiled.append(self._compile_entry(entry))
        self._compiled = compiled

    def _compile_entry(self, entry: WordEntry) -> CompiledWord:
        normalized = entry.normalized or normalize_reference(entry.pattern)
        entry.normalized = normalized
        if not entry.keywords:
            forms = [normalized]
            skeleton = text_variants(normalized).skeleton.text
            if skeleton and skeleton not in forms:
                forms.append(skeleton)
            entry.keywords = tuple(forms)

        obfuscation_regex = None
        if entry.effective_match_type == "regex":
            try:
                obfuscation_regex = re.compile(entry.pattern, BYPASS_FLAGS)
            except re.error:
                self._warn_invalid(entry.pattern)

        return CompiledWord(entry=entry, obfuscation_regex=obfuscation_regex)

    def _warn_invalid(self, pattern: str) -> None:
        if pattern not in _invalid_patterns_logged:
            _invalid_patterns_logged.add(pattern)
            _invalid_patterns.add(pattern)
            logger.warning(
                f"[wordfilter] Eintrag {pattern!r} ist kein gueltiger regulaerer "
                "Ausdruck und wird uebersprungen."
            )

    # -- Zugriff --
    @property
    def entry_count(self) -> int:
        return len(self._compiled)

    def patterns(self) -> List[str]:
        return [compiled.entry.pattern for compiled in self._compiled]

    # -- Pruefung --
    def check(self, text: str) -> Optional[WordMatch]:
        """Erster (bester) Treffer oder None."""
        variants = text_variants(text or "")
        if not variants.alpha.text and not variants.norm:
            return None

        tokens = tokenize(variants.norm) if variants.norm else []
        fallback: Optional[WordMatch] = None

        for compiled in self._compiled:
            match = self._check_entry(compiled, variants, tokens)
            if match is None:
                continue
            if match.method in ("exact", "regex"):
                return match
            if fallback is None:
                fallback = match
        return fallback

    def check_all(self, text: str, limit: int = 20) -> List[WordMatch]:
        """Alle Treffer (fuer /raid wordfilter test)."""
        variants = text_variants(text or "")
        tokens = tokenize(variants.norm) if variants.norm else []
        found: List[WordMatch] = []
        for compiled in self._compiled:
            match = self._check_entry(compiled, variants, tokens)
            if match is not None:
                found.append(match)
                if len(found) >= limit:
                    break
        return found

    # -- interne Logik --
    def _check_entry(
        self,
        compiled: CompiledWord,
        variants: TextVariants,
        tokens: Sequence,
    ) -> Optional[WordMatch]:
        entry = compiled.entry
        match_type = entry.effective_match_type
        candidates = _candidate_forms(entry)

        if match_type == "regex":
            return self._check_regex(compiled, variants)

        if match_type == "word":
            for token in tokens:
                if not _token_matches(token.text, candidates):
                    continue
                if self._context_blocks(entry, variants, token.start, token.end, token.text):
                    continue
                if _is_whitelisted(
                    entry, token.text, token.text, variants.norm, token.start, token.end
                ):
                    continue
                return WordMatch(
                    entry=entry,
                    method="exact",
                    reason=f"Wort `{token.text}`",
                    confidence=1.0,
                    span=(token.start, token.end),
                    matched_text=token.text,
                )
            return self._check_obfuscated(compiled, variants)

        if match_type == "token":
            for token in tokens:
                alpha_token = _alpha_token(variants.norm, token.start, token.end)
                for piece in self._token_pieces(entry, alpha_token):
                    if not _token_matches(piece, candidates):
                        continue
                    start, end = token.start, token.end
                    if entry.normalized.isdigit() and _digit_neighbors(variants.norm, start, end):
                        continue
                    if self._context_blocks(entry, variants, start, end, piece):
                        continue
                    if _is_whitelisted(
                        entry, piece, alpha_token, variants.norm, start, end
                    ):
                        continue
                    return WordMatch(
                        entry=entry,
                        method="exact",
                        reason=f"Token `{piece}`",
                        confidence=1.0,
                        span=(start, end),
                        matched_text=piece,
                    )
            return self._check_obfuscated(compiled, variants)

        # substring
        return self._check_substring(compiled, variants)

    @staticmethod
    def _token_pieces(entry: WordEntry, alpha_token: str) -> List[str]:
        """
        Zerlegt ein Token in vergleichbare Stuecke.

        Fuer Zahlen werden zusaetzlich alle aneinanderhaengenden Ziffernbloecke
        gebildet, damit "1234" nicht auf "12345" passt, "1.2.3.4" aber den
        Eintrag "1234" treffen kann.
        """
        pieces: List[str] = [alpha_token]
        if entry.normalized.isdigit():
            for match in re.finditer(r"\d+", alpha_token):
                block = match.group(0)
                if block != alpha_token and block not in pieces:
                    pieces.append(block)
        return pieces

    def _check_substring(
        self, compiled: CompiledWord, variants: TextVariants
    ) -> Optional[WordMatch]:
        entry = compiled.entry
        wanted = entry.normalized
        if not wanted:
            return None

        if wanted.isdigit():
            # Zahlen nur als kompletter Ziffernblock.
            for match in re.finditer(r"\d+", variants.alpha.text):
                if match.group(0) != wanted:
                    continue
                start, end = variants.alpha.span_to_norm(match.start(), match.end())
                if self._context_blocks(entry, variants, start, end, wanted):
                    continue
                if _is_whitelisted(entry, wanted, wanted, variants.norm, start, end):
                    continue
                return WordMatch(
                    entry=entry,
                    method="substring",
                    reason=f"Zahl `{wanted}`",
                    confidence=0.95,
                    span=(start, end),
                    matched_text=wanted,
                )
            return self._check_obfuscated(compiled, variants)

        for piece in _candidate_forms(entry):
            index = variants.alpha.text.find(piece)
            if index == -1:
                continue
            start, end = variants.alpha.span_to_norm(index, index + len(piece))
            if self._context_blocks(entry, variants, start, end, piece):
                continue
            if _is_whitelisted(entry, piece, piece, variants.norm, start, end):
                continue
            return WordMatch(
                entry=entry,
                method="substring",
                reason=f"Teilstring `{piece}`",
                confidence=0.8,
                span=(start, end),
                matched_text=piece,
            )
        return self._check_obfuscated(compiled, variants)

    def _check_regex(
        self, compiled: CompiledWord, variants: TextVariants
    ) -> Optional[WordMatch]:
        entry = compiled.entry
        regex = compiled.obfuscation_regex
        if regex is None:
            self._warn_invalid(entry.pattern)
            return None

        subject = variants.norm
        try:
            if TIMEOUT is not None:
                match = regex.search(subject, timeout=REGEX_TIMEOUT_SECONDS)
            else:  # pragma: no cover
                match = regex.search(subject)
        except TimeoutError:
            logger.warning(
                f"[wordfilter] Regex {entry.pattern!r} hat das Zeitlimit ueberschritten."
            )
            return None
        except re.error as error:
            self._warn_invalid(entry.pattern)
            logger.debug(f"[wordfilter] Regex-Fehler: {error}")
            return None
        if match is None:
            return None

        start, end = match.span()
        if start == end:
            return None
        found = match.group(0)
        if self._context_blocks(entry, variants, start, end, found):
            return None
        if _is_whitelisted(
            entry, found, _alpha_token(subject, start, end), subject, start, end
        ):
            return None
        return WordMatch(
            entry=entry,
            method="regex",
            reason=f"Regex `{entry.pattern}`",
            confidence=0.9,
            span=(start, end),
            matched_text=found,
        )

    def _check_obfuscated(
        self, compiled: CompiledWord, variants: TextVariants
    ) -> Optional[WordMatch]:
        """
        Letzte Stufe: Trenner, Leetspeak, Homoglyphe, Zeichenwiederholungen.

        Verglichen wird die Skeleton-Sicht der Nachricht mit dem Skeleton des
        Musters.  Geprueft wird auf der Alpha-Sicht (ohne Trenner); dadurch
        werden auch Umgehungen erkannt, die sich ueber mehrere Tokens
        erstrecken ("b.a.d.w.o.r.d", "b-a-d-w-o-r-d", "b a d w o r d").
        """
        entry = compiled.entry
        if not entry.fuzzy or len(entry.normalized) < 3:
            return None
        # Zahlen werden nie unscharf verglichen: "123" darf nicht in "1234"
        # oder "4123" treffen.
        if entry.normalized.isdigit():
            return None

        candidates = _candidate_forms(entry)
        # Geprueft wird auf der Alpha-Sicht (ohne Trenner) in der die
        # Skeleton-Variante eingebettet ist.  Dadurch werden auch Muster
        # erkannt, die sich ueber mehrere Tokens erstrecken:
        # "b.a.d.w.o.r.d", "b-a-d-w-o-r-d" oder "b a d w o r d".
        collected = self._collect_alpha(variants.skeleton.text)
        if not collected:
            return None

        for candidate in candidates:
            index = collected.text.find(candidate)
            if index == -1:
                continue
            span = self._obfuscation_span(
                variants.norm, collected, index, len(candidate)
            )
            if span is None:
                # Kein zusammenhaengendes Muster (z. B. Zufallstreffer ueber
                # mehrere Woerter wie "die insel") - keine Umgehung.
                continue
            start, end = span
            found = variants.norm[start:end]
            if self._is_single_run(variants.norm, start, end):
                # Zufaelliger Teilstring in einem normalen Wort
                # ("ass" in "class") - keine Umgehung.
                continue
            if normalize_reference(found) == entry.normalized:
                # Direkter Treffer, bereits in Stufe 1 behandelt.
                continue
            if self._context_blocks(entry, variants, start, end, found):
                continue
            if _is_whitelisted(entry, found, found, variants.norm, start, end):
                continue
            # Je mehr zusaetzliche Zeichen (Trenner, Wiederholungen) im
            # Original stehen, desto unsicherer ist der Treffer.
            extra = max(0, len(found) - len(candidate))
            confidence = 0.9 if extra == 0 else max(0.7, 0.9 - extra / (2 * len(found)))
            return WordMatch(
                entry=entry,
                method="obfuscated",
                reason=f"Umgehung von `{entry.pattern}` als `{found}`",
                confidence=confidence,
                span=(start, end),
                matched_text=found,
            )
        return None

    @staticmethod
    def _obfuscation_span(
        norm: str, collected: "_Collected", index: int, length: int, max_gap: int = 3
    ) -> Optional[Tuple[int, int]]:
        """
        Liefert den Originalbereich eines Treffers - aber nur, wenn er wie
        eine Umgehung aussieht.  Sonst None.

        Eine Umgehung erkennt man an dem, was ZWISCHEN den Trefferzeichen
        steht:

          "b.a.d"   -> zwischen jedem Buchstaben ein Trenner  => Umgehung
          "b a d"   -> nur Leerzeichen                        => Umgehung
          "b  a  d" -> kleine Luecken                         => Umgehung
          "die insel" -> Treffer "insel" liegt komplett in einem Wort, die
                        Luecke liegt *vor* dem Treffer       => keine Umgehung

        Deshalb werden ausschliesslich die Luecken zwischen erstem und letztem
        Trefferzeichen betrachtet.  Keine Luecke darf laenger als `max_gap`
        sein, die Luecken zusammen nicht laenger als das Muster.
        """
        if not collected.positions:
            return None
        first = max(0, min(index, len(collected.positions) - 1))
        last = max(0, min(index + length - 1, len(collected.positions) - 1))
        if last < first:
            return None

        start = collected.positions[first]
        end = collected.positions[last] + 1
        if end <= start:
            return None
        noise = sum(
            1 for char in norm[start:end] if not char.isalnum()
        )
        if noise > length:
            return None
        if last > first:
            for offset in range(first, last):
                gap = collected.positions[offset + 1] - collected.positions[offset] - 1
                if gap < 0 or gap > max_gap:
                    return None
        return start, end

    @staticmethod
    def _is_single_run(norm: str, start: int, end: int) -> bool:
        """
        True, wenn der Bereich komplett in einem zusammenhaengenden
        alphanumerischen Block liegt.

        Solche Treffer sind fast immer Zufall: "ass" steckt als Teilstring in
        "class" und "passage".  Eine echte Umgehung erstreckt sich dagegen
        ueber mehrere Bloecke ("b.a.d", "b a d", "b-a-d").
        """
        if start >= end:
            return False
        return norm[start:end].isalnum()

    @staticmethod
    def _collect_alpha(subject: str) -> "_Collected":
        """
        Sammelt die alphanumerischen Zeichen eines Textes und merkt, an
        welcher Stelle sie standen.  So kann ein Treffer auf den normalen
        Text zurueckgerechnet werden.
        """
        chars: List[str] = []
        positions: List[int] = []
        for index, char in enumerate(subject):
            if char.isalnum():
                chars.append(char)
                positions.append(index)
        return _Collected("".join(chars), positions)

    @staticmethod
    def _context_blocks(
        entry: WordEntry,
        variants: TextVariants,
        start: int,
        end: int,
        found: str,
    ) -> bool:
        norm = variants.norm
        if entry.bypass_urls and _is_within_url(norm, start, end):
            return True
        if entry.bypass_code and _is_within_code(norm, start, end):
            return True
        if entry.bypass_urls and _looks_like_url_fragment(found):
            return True
        return False


def _looks_like_url_fragment(found: str) -> bool:
    """
    Erkennt einen Domain-artigen Treffer ("evil.com"), damit ein verbotenes
    kurzes Wort nicht in einer Domain anschlaegt.

    Wichtig: alle Labels muessen mindestens zwei Zeichen haben.  Sonst wuerde
    eine Umgehung wie "b.a.d.w.o.r.d" (nur einzelne Buchstaben zwischen den
    Punkten) als Domain durchgehen und der Filter waere wirkungslos.
    """
    if "." not in found or not is_ascii(found):
        return False
    labels = [part for part in found.split(".") if part]
    if len(labels) < 2 or any(len(label) < 2 for label in labels):
        return False
    return all(label.isalnum() or "-" in label for label in labels)


# ── Globale Eintrags-Verwaltung (Cache fuer die Listener) ───────────────────

_ENTRIES_VERSION = 0
_ENTRIES: Dict[str, Tuple[int, List[WordEntry]]] = {}


def refresh_entries_cache(entries: Iterable[WordEntry], allow: Iterable[str]) -> int:
    """
    Setzt fuer alle uebergebenen Eintraege die abgeleiteten Felder und die
    Whitelist.  Gibt die Anzahl der Eintraege zurueck.

    Die Whitelist wird bewusst auf jeden Eintrag angewendet: ein Whitelist-Wort
    verhindert den Treffer des gleichnamigen Filter-Worts.
    """
    prepared: List[WordEntry] = []
    for entry in entries:
        entry.normalized = normalize_reference(entry.pattern)
        forms = [entry.normalized]
        skeleton = text_variants(entry.normalized).skeleton.text
        if skeleton and skeleton not in forms:
            forms.append(skeleton)
        entry.keywords = tuple(forms)
        entry.whitelist = tuple({normalize_reference(word) for word in allow if word})
        prepared.append(entry)
    return len(prepared)


def attach_whitelist(entries: Iterable[WordEntry], allow: Iterable[str]) -> None:
    """Setzt nur die Whitelist (fuer Tests und den Test-Command)."""
    normalized_allow = tuple({normalize_reference(word) for word in allow if word})
    for entry in entries:
        entry.whitelist = normalized_allow


def build_matcher(entries: Iterable[WordEntry], allow: Iterable[str] = ()) -> WordMatcher:
    """Baut einen einsatzbereiten Matcher aus Eintraegen und Whitelist."""
    prepared = [
        WordEntry(
            pattern=entry.pattern,
            match_type=entry.effective_match_type,
            action=entry.action,
            severity=entry.severity,
            fuzzy=entry.fuzzy,
            bypass_urls=entry.bypass_urls,
            bypass_code=entry.bypass_code,
            note=entry.note,
            enabled=entry.enabled,
            entry_id=entry.entry_id,
            hit_count=entry.hit_count,
            normalized=entry.normalized or normalize_reference(entry.pattern),
        )
        for entry in entries
    ]
    attach_whitelist(prepared, allow)
    matcher = WordMatcher(entries=prepared)
    matcher.compile()
    return matcher


def reduce_text(text: str) -> str:
    """Hilfsfunktion fuer Debug-Ausgaben: die Skeleton-Sicht eines Textes."""
    return text_variants(text).skeleton.text


def describe_characters(text: str) -> str:
    """Debug: zeigt Zeichen samt Unicode-Namen (fuer Homoglyph-Analysen)."""
    parts: List[str] = []
    for char in text:
        if char.isascii():
            parts.append(char)
        else:
            try:
                name = unicodedata.name(char)
            except ValueError:
                name = "UNBEKANNT"
            parts.append(f"[{char}={name}]")
    return " ".join(parts)
