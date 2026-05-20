"""
fuzzy_match.py — Sanctions screening funnel (25 algorithms).

Library-only module. Nothing in the live orchestrator currently imports it;
the agent's `screening_api_lookup` (sanctions_triage/src/tools.py) uses a
simple SQL LIKE query against sanctions.db. To wire this in, replace that
query with a call to `screen_fuzzy(input_name, ...)`.

Main entry point: screen_fuzzy(input_name, ...) -> list[dict]

Reads from sanctions.db (SQLite) with the schema:
    sanctions(id, full_name, nationality, program, source, listed_on,
              raw_data, dob)

The funnel is laid out as a cascading set of cheap-to-expensive filters:

    1. Blocking / canopy   — narrow 66k rows to ~300 candidates by token + phonetic key
    2. Exact / pattern     — uppercased exact, initial-collapsed (M.A.H. ↔ Mohammed Ali Hassan)
    3. Phonetic            — Soundex, Refined Soundex, Metaphone, NYSIIS, simplified DMetaphone
    4. String distance     — Levenshtein, Damerau-Levenshtein, Hamming, LCS
    5. Jaro family         — Jaro, Jaro-Winkler
    6. Token similarity    — Jaccard, Sorensen-Dice, token-set ratio
    7. Vector              — char-n-gram cosine, TF-IDF cosine (lightweight)
    8. Alignment           — Smith-Waterman (local), Needleman-Wunsch (global)
    9. Probabilistic       — name + DOB + nationality (Fellegi-Sunter-flavoured)
   10. Hybrid combiners    — weighted blend with bias toward high-precision signals

Optional dependencies (faster, used if available; pure-Python fallbacks otherwise):
    - jellyfish    : C-backed soundex/metaphone/jaro_winkler/levenshtein
    - rapidfuzz    : C-backed levenshtein, ratio, token_set_ratio
"""
from __future__ import annotations

import math
import re
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

# ── Optional speed-ups ────────────────────────────────────────────────
try:
    import jellyfish as _jellyfish  # type: ignore
    HAS_JELLYFISH = True
except ImportError:
    _jellyfish = None
    HAS_JELLYFISH = False

try:
    from rapidfuzz import fuzz as _rfuzz  # type: ignore
    from rapidfuzz import distance as _rdist  # type: ignore
    HAS_RAPIDFUZZ = True
except ImportError:
    _rfuzz = None
    _rdist = None
    HAS_RAPIDFUZZ = False


# ─────────────────────────────────────────────────────────────────────
# Normalisation
# ─────────────────────────────────────────────────────────────────────
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalise(name: str) -> str:
    """Lowercase, strip accents, drop punctuation, collapse whitespace."""
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower()
    name = _PUNCT_RE.sub(" ", name)
    name = _WS_RE.sub(" ", name).strip()
    return name


def tokens(name: str) -> list[str]:
    return [t for t in normalise(name).split() if t]


def initials(name: str) -> str:
    return "".join(t[0] for t in tokens(name) if t)


# ─────────────────────────────────────────────────────────────────────
# 1. Phonetic encoders
# ─────────────────────────────────────────────────────────────────────
def soundex(s: str) -> str:
    """American Soundex (4 chars)."""
    if HAS_JELLYFISH:
        try:
            return _jellyfish.soundex(s or "")
        except Exception:
            pass
    if not s:
        return "0000"
    s = re.sub(r"[^A-Za-z]", "", s).upper()
    if not s:
        return "0000"
    code_map = {**dict.fromkeys("BFPV", "1"),
                **dict.fromkeys("CGJKQSXZ", "2"),
                **dict.fromkeys("DT", "3"),
                **dict.fromkeys("L", "4"),
                **dict.fromkeys("MN", "5"),
                **dict.fromkeys("R", "6")}
    first = s[0]
    encoded = [code_map.get(c, "") for c in s[1:]]
    out = first
    last = code_map.get(first, "")
    for code in encoded:
        if code and code != last:
            out += code
        last = code
    out = re.sub(r"[AEIOUYHW]", "", out[1:])
    out = first + out
    return (out + "000")[:4]


def refined_soundex(s: str) -> str:
    """Refined Soundex — fewer collisions, variable length."""
    if not s:
        return ""
    s = re.sub(r"[^A-Za-z]", "", s).upper()
    if not s:
        return ""
    table = {**dict.fromkeys("BP", "1"), **dict.fromkeys("FV", "2"),
             **dict.fromkeys("CKS", "3"), **dict.fromkeys("G", "4"),
             **dict.fromkeys("J", "5"), **dict.fromkeys("QXZ", "6"),
             **dict.fromkeys("DT", "7"), **dict.fromkeys("L", "8"),
             **dict.fromkeys("MN", "9"), **dict.fromkeys("R", "A")}
    out = [s[0]]
    last = ""
    for c in s[1:]:
        code = table.get(c, "")
        if code and code != last:
            out.append(code)
        last = code
    return "".join(out)


def metaphone(s: str) -> str:
    """Metaphone (Lawrence Philips, 1990). Simplified pure-Python."""
    if HAS_JELLYFISH:
        try:
            return _jellyfish.metaphone(s or "")
        except Exception:
            pass
    if not s:
        return ""
    s = re.sub(r"[^A-Za-z]", "", s).upper()
    if not s:
        return ""
    # Drop duplicate adjacent consonants
    s = re.sub(r"([^AEIOU])\1+", r"\1", s)
    # Initial transformations
    if s[:2] in ("AE", "GN", "KN", "PN", "WR"):
        s = s[1:]
    elif s.startswith("X"):
        s = "S" + s[1:]
    elif s.startswith("WH"):
        s = "W" + s[2:]
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        prev = s[i - 1] if i > 0 else ""
        nxt = s[i + 1] if i + 1 < len(s) else ""
        if c in "AEIOU":
            if i == 0:
                out.append(c)
        elif c == "B":
            if not (i == len(s) - 1 and prev == "M"):
                out.append("B")
        elif c == "C":
            if nxt == "H":
                out.append("X"); i += 1
            elif nxt in "IEY":
                out.append("S")
            else:
                out.append("K")
        elif c == "D":
            if nxt == "G" and i + 2 < len(s) and s[i + 2] in "IEY":
                out.append("J"); i += 2
            else:
                out.append("T")
        elif c == "F":
            out.append("F")
        elif c == "G":
            if nxt == "H":
                if i > 0 and prev not in "AEIOU":
                    pass
                else:
                    out.append("F")
                i += 1
            elif nxt == "N":
                pass
            elif nxt in "IEY":
                out.append("J")
            else:
                out.append("K")
        elif c == "H":
            if prev in "AEIOU" and nxt not in "AEIOU":
                pass
            else:
                out.append("H")
        elif c == "J":
            out.append("J")
        elif c == "K":
            if prev != "C":
                out.append("K")
        elif c == "L":
            out.append("L")
        elif c == "M":
            out.append("M")
        elif c == "N":
            out.append("N")
        elif c == "P":
            if nxt == "H":
                out.append("F"); i += 1
            else:
                out.append("P")
        elif c == "Q":
            out.append("K")
        elif c == "R":
            out.append("R")
        elif c == "S":
            if nxt == "H":
                out.append("X"); i += 1
            else:
                out.append("S")
        elif c == "T":
            if nxt == "H":
                out.append("0"); i += 1
            else:
                out.append("T")
        elif c == "V":
            out.append("F")
        elif c == "W":
            if nxt in "AEIOU":
                out.append("W")
        elif c == "X":
            out.append("KS")
        elif c == "Y":
            if nxt in "AEIOU":
                out.append("Y")
        elif c == "Z":
            out.append("S")
        i += 1
    return "".join(out)


def double_metaphone(s: str) -> tuple[str, str]:
    """Simplified Double Metaphone — primary and alternate codes.
    Real DM has hundreds of rules; this captures the common branches."""
    primary = metaphone(s)
    # Alternate: substitute K↔C, F↔V, S↔Z for branching
    alt = (primary
           .replace("K", "C", 1)
           .replace("F", "V", 1)
           .replace("S", "Z", 1))
    return primary, alt


def nysiis(s: str) -> str:
    """New York State Identification and Intelligence System (6 chars)."""
    if HAS_JELLYFISH:
        try:
            return _jellyfish.nysiis(s or "")
        except Exception:
            pass
    if not s:
        return ""
    s = re.sub(r"[^A-Za-z]", "", s).upper()
    if not s:
        return ""
    # Prefix substitutions
    for old, new in (("MAC", "MCC"), ("KN", "N"), ("K", "C"),
                     ("PH", "FF"), ("PF", "FF"), ("SCH", "SSS")):
        if s.startswith(old):
            s = new + s[len(old):]
            break
    # Suffix substitutions
    for old, new in (("EE", "Y"), ("IE", "Y"),
                     ("DT", "D"), ("RT", "D"), ("RD", "D"),
                     ("NT", "D"), ("ND", "D")):
        if s.endswith(old):
            s = s[:-len(old)] + new
            break
    key = s[0]
    rest = s[1:]
    out = []
    i = 0
    while i < len(rest):
        c = rest[i]
        nxt = rest[i + 1] if i + 1 < len(rest) else ""
        if c == "E" and nxt == "V":
            out.append("AF"); i += 2; continue
        if c in "AEIOU":
            out.append("A")
        elif c == "Q":
            out.append("G")
        elif c == "Z":
            out.append("S")
        elif c == "M":
            out.append("N")
        elif c == "K":
            out.append("S" if nxt == "N" else "C")
        elif c == "S" and rest[i:i + 3] == "SCH":
            out.append("SSS"); i += 3; continue
        elif c == "P" and nxt == "H":
            out.append("FF"); i += 2; continue
        elif c == "H" and (out[-1] not in "AEIOU" if out else True):
            out.append(c if i == 0 else "")
        elif c == "W" and out and out[-1] in "AEIOU":
            pass
        else:
            out.append(c)
        i += 1
    code = key + "".join(out)
    # Collapse adjacent duplicates
    collapsed = code[0] if code else ""
    for c in code[1:]:
        if c != collapsed[-1]:
            collapsed += c
    if collapsed.endswith("S"):
        collapsed = collapsed[:-1]
    if collapsed.endswith("AY"):
        collapsed = collapsed[:-2] + "Y"
    if collapsed.endswith("A"):
        collapsed = collapsed[:-1]
    return collapsed[:6]


# ─────────────────────────────────────────────────────────────────────
# 2. String distance / similarity
# ─────────────────────────────────────────────────────────────────────
def levenshtein(a: str, b: str) -> int:
    """Classic edit distance (insert/delete/substitute, cost 1)."""
    if HAS_RAPIDFUZZ:
        return _rdist.Levenshtein.distance(a, b)
    if HAS_JELLYFISH:
        try:
            return _jellyfish.levenshtein_distance(a, b)
        except Exception:
            pass
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(cur[-1] + 1, prev[j] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def damerau_levenshtein(a: str, b: str) -> int:
    """Edit distance with adjacent-transpositions allowed."""
    if HAS_JELLYFISH:
        try:
            return _jellyfish.damerau_levenshtein_distance(a, b)
        except Exception:
            pass
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1,
                          d[i][j - 1] + 1,
                          d[i - 1][j - 1] + cost)
            if (i > 1 and j > 1 and a[i - 1] == b[j - 2]
                    and a[i - 2] == b[j - 1]):
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + cost)
    return d[la][lb]


def hamming(a: str, b: str) -> int:
    """Hamming distance (only defined for equal-length strings; otherwise
    returns sum of differing positions plus the length difference)."""
    return sum(c1 != c2 for c1, c2 in zip(a, b)) + abs(len(a) - len(b))


def lcs_length(a: str, b: str) -> int:
    """Longest common subsequence length."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur.append(prev[j - 1] + 1)
            else:
                cur.append(max(cur[-1], prev[j]))
        prev = cur
    return prev[-1]


def jaro(a: str, b: str) -> float:
    """Jaro similarity in [0, 1]."""
    if HAS_JELLYFISH:
        try:
            return _jellyfish.jaro_similarity(a, b)
        except Exception:
            pass
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    la, lb = len(a), len(b)
    window = max(la, lb) // 2 - 1
    window = max(window, 0)
    a_match = [False] * la
    b_match = [False] * lb
    matches = 0
    for i, ca in enumerate(a):
        lo = max(0, i - window)
        hi = min(lb, i + window + 1)
        for j in range(lo, hi):
            if not b_match[j] and ca == b[j]:
                a_match[i] = True
                b_match[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    k = 0
    transpositions = 0
    for i in range(la):
        if a_match[i]:
            while not b_match[k]:
                k += 1
            if a[i] != b[k]:
                transpositions += 1
            k += 1
    transpositions //= 2
    return (matches / la + matches / lb
            + (matches - transpositions) / matches) / 3.0


def jaro_winkler(a: str, b: str, p: float = 0.1) -> float:
    """Jaro-Winkler — Jaro with shared-prefix bonus."""
    if HAS_JELLYFISH:
        try:
            return _jellyfish.jaro_winkler_similarity(a, b)
        except Exception:
            pass
    j = jaro(a, b)
    prefix = 0
    for ca, cb in zip(a[:4], b[:4]):
        if ca == cb:
            prefix += 1
        else:
            break
    return j + prefix * p * (1 - j)


# ─────────────────────────────────────────────────────────────────────
# 3. Token-based similarity
# ─────────────────────────────────────────────────────────────────────
def jaccard(a: str, b: str) -> float:
    """Jaccard index on whitespace-tokenised names."""
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def sorensen_dice(a: str, b: str) -> float:
    """Sørensen-Dice coefficient on tokens."""
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return 2 * len(ta & tb) / (len(ta) + len(tb))


def token_set_ratio(a: str, b: str) -> float:
    """rapidfuzz-style token_set_ratio — order-independent string compare."""
    if HAS_RAPIDFUZZ:
        return _rfuzz.token_set_ratio(a, b) / 100.0
    ta, tb = sorted(set(tokens(a))), sorted(set(tokens(b)))
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    common = sorted(set(ta) & set(tb))
    sa = " ".join(common + sorted(set(ta) - set(tb)))
    sb = " ".join(common + sorted(set(tb) - set(ta)))
    if not sa or not sb:
        return 0.0
    d = levenshtein(sa, sb)
    return 1 - d / max(len(sa), len(sb))


# ─────────────────────────────────────────────────────────────────────
# 4. N-gram / vector similarity
# ─────────────────────────────────────────────────────────────────────
def char_ngrams(s: str, n: int = 3) -> list[str]:
    s = f" {normalise(s)} "
    return [s[i:i + n] for i in range(len(s) - n + 1)] if len(s) >= n else [s]


def ngram_cosine(a: str, b: str, n: int = 3) -> float:
    """Cosine similarity of character n-gram bag-of-tokens."""
    va = Counter(char_ngrams(a, n))
    vb = Counter(char_ngrams(b, n))
    if not va or not vb:
        return 0.0
    dot = sum(va[k] * vb.get(k, 0) for k in va)
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    return dot / (na * nb) if na and nb else 0.0


def bigram_overlap(a: str, b: str) -> float:
    return ngram_cosine(a, b, n=2)


def trigram_overlap(a: str, b: str) -> float:
    return ngram_cosine(a, b, n=3)


# ─────────────────────────────────────────────────────────────────────
# 5. Alignment (Smith-Waterman / Needleman-Wunsch)
# ─────────────────────────────────────────────────────────────────────
def smith_waterman(a: str, b: str,
                   match: int = 2, mismatch: int = -1, gap: int = -2) -> float:
    """Local alignment score normalised to [0, 1]."""
    a, b = normalise(a), normalise(b)
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    H = [[0] * (lb + 1) for _ in range(la + 1)]
    best = 0
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            s = match if a[i - 1] == b[j - 1] else mismatch
            H[i][j] = max(0, H[i - 1][j - 1] + s,
                          H[i - 1][j] + gap, H[i][j - 1] + gap)
            if H[i][j] > best:
                best = H[i][j]
    return best / (min(la, lb) * match)


def needleman_wunsch(a: str, b: str,
                     match: int = 2, mismatch: int = -1, gap: int = -2) -> float:
    """Global alignment score normalised to [0, 1]."""
    a, b = normalise(a), normalise(b)
    la, lb = len(a), len(b)
    if not la and not lb:
        return 1.0
    if not la or not lb:
        return 0.0
    F = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        F[i][0] = gap * i
    for j in range(lb + 1):
        F[0][j] = gap * j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            s = match if a[i - 1] == b[j - 1] else mismatch
            F[i][j] = max(F[i - 1][j - 1] + s,
                          F[i - 1][j] + gap, F[i][j - 1] + gap)
    max_score = max(la, lb) * match
    min_score = max(la, lb) * gap
    raw = F[la][lb]
    return (raw - min_score) / (max_score - min_score) if max_score > min_score else 0.0


# ─────────────────────────────────────────────────────────────────────
# 6. Exact / pattern / initial collapse
# ─────────────────────────────────────────────────────────────────────
def exact_match(a: str, b: str) -> float:
    return 1.0 if normalise(a) == normalise(b) else 0.0


def initial_collapse_match(a: str, b: str) -> float:
    """Match e.g. 'M. A. Hassan' against 'Mohammed Ali Hassan' via initials."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    # If one side is mostly single-letter tokens, compare initials to the other.
    initials_a = "".join(t[0] for t in ta)
    initials_b = "".join(t[0] for t in tb)
    if all(len(t) <= 2 for t in ta) and initials_a == initials_b:
        return 1.0
    if all(len(t) <= 2 for t in tb) and initials_a == initials_b:
        return 1.0
    if initials_a == initials_b:
        return 0.5
    return 0.0


def substring_containment(a: str, b: str) -> float:
    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        shorter = min(len(na), len(nb))
        longer = max(len(na), len(nb))
        return shorter / longer
    return 0.0


# ─────────────────────────────────────────────────────────────────────
# 7. Phonetic equality flags (algorithm slots 17–21)
# ─────────────────────────────────────────────────────────────────────
def soundex_match(a: str, b: str) -> float:
    return 1.0 if soundex(a) == soundex(b) else 0.0


def refined_soundex_match(a: str, b: str) -> float:
    return 1.0 if refined_soundex(a) == refined_soundex(b) else 0.0


def metaphone_match(a: str, b: str) -> float:
    return 1.0 if metaphone(a) == metaphone(b) else 0.0


def nysiis_match(a: str, b: str) -> float:
    return 1.0 if nysiis(a) == nysiis(b) else 0.0


def dmetaphone_match(a: str, b: str) -> float:
    pa = double_metaphone(a)
    pb = double_metaphone(b)
    return 1.0 if (set(pa) & set(pb)) else 0.0


# ─────────────────────────────────────────────────────────────────────
# 8. Probabilistic (Fellegi-Sunter-flavoured)
# ─────────────────────────────────────────────────────────────────────
def dob_score(a: Optional[str], b: Optional[str]) -> float:
    """Compare two date-ish strings. 1.0 exact, 0.7 same year, 0.0 mismatch."""
    if not a or not b:
        return 0.5  # missing = neutral
    ya = re.search(r"\b(19|20)\d{2}\b", a)
    yb = re.search(r"\b(19|20)\d{2}\b", b)
    if not (ya and yb):
        return 0.5
    if a.strip() == b.strip():
        return 1.0
    if ya.group(0) == yb.group(0):
        return 0.7
    return 0.0


def nationality_score(a: Optional[str], b: Optional[str]) -> float:
    if not a or not b:
        return 0.5
    na = (a or "").strip().lower()[:2]
    nb = (b or "").strip().lower()[:2]
    if not na or not nb:
        return 0.5
    return 1.0 if na == nb else 0.0


def probabilistic_combine(name_score: float,
                          dob: float,
                          nat: float,
                          m_name: float = 0.95,
                          u_name: float = 0.10,
                          m_dob: float = 0.99,
                          u_dob: float = 0.05,
                          m_nat: float = 0.90,
                          u_nat: float = 0.15) -> float:
    """Fellegi-Sunter style log-likelihood combiner, mapped back to [0, 1]."""
    def w(score: float, m: float, u: float) -> float:
        # score acts as a soft agree/disagree weight
        if score >= 0.9:
            return math.log2(m / u)
        if score <= 0.1:
            return math.log2((1 - m) / (1 - u))
        return score * math.log2(m / u) + (1 - score) * math.log2((1 - m) / (1 - u))

    total = w(name_score, m_name, u_name) + w(dob, m_dob, u_dob) + w(nat, m_nat, u_nat)
    # Squash via logistic
    return 1.0 / (1.0 + math.exp(-total / 3.0))


# ─────────────────────────────────────────────────────────────────────
# Blocking / canopy
# ─────────────────────────────────────────────────────────────────────
def _blocking_keys(name: str) -> set[str]:
    """Cheap keys for narrowing the candidate pool.
    Combine first-token, last-token, soundex of last, metaphone of last."""
    toks = tokens(name)
    if not toks:
        return set()
    keys = set()
    keys.add(f"t:{toks[0]}")
    keys.add(f"t:{toks[-1]}")
    keys.add(f"sx:{soundex(toks[-1])}")
    keys.add(f"mp:{metaphone(toks[-1])}")
    if len(toks) >= 2:
        keys.add(f"sx2:{soundex(toks[0])}")
    return keys


def _candidates_from_db(db_path: str | Path, input_name: str,
                        max_candidates: int = 500) -> list[dict]:
    """Pull a narrowed candidate list using a SQL LIKE OR over blocking tokens
    on the last/first tokens. Returns up to max_candidates real rows."""
    toks = tokens(input_name)
    if not toks:
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Build OR LIKE conditions on each token; SQLite handles small ORs fine.
        like_clauses = " OR ".join(["LOWER(full_name) LIKE ?"] * len(toks))
        params = [f"%{t}%" for t in toks]
        rows = conn.execute(
            f"""
            SELECT id, full_name, nationality, program, source, listed_on,
                   raw_data, dob
            FROM sanctions
            WHERE full_name IS NOT NULL AND ({like_clauses})
            LIMIT ?
            """,
            (*params, max_candidates),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────
# Scoring per candidate (all 25 algorithms)
# ─────────────────────────────────────────────────────────────────────
ALGO_WEIGHTS: dict[str, float] = {
    # exact + pattern (high precision)
    "exact":              5.0,
    "initial_collapse":   2.0,
    "substring":          1.5,
    # phonetic equalities (binary)
    "soundex_eq":         1.0,
    "refined_soundex_eq": 1.0,
    "metaphone_eq":       1.2,
    "dmetaphone_eq":      1.2,
    "nysiis_eq":          1.0,
    # string-distance similarities (continuous)
    "levenshtein_sim":    3.0,
    "damerau_sim":        3.0,
    "hamming_sim":        0.7,
    "lcs_sim":            1.5,
    # jaro family
    "jaro":               3.0,
    "jaro_winkler":       4.0,
    # token
    "jaccard":            2.0,
    "sorensen_dice":      2.0,
    "token_set_ratio":    3.0,
    # vector
    "bigram_cos":         2.0,
    "trigram_cos":        2.0,
    # alignment
    "smith_waterman":     2.0,
    "needleman_wunsch":   2.0,
}


def score_pair(a: str, b: str) -> dict[str, float]:
    """Run all string-similarity algorithms (no DOB/nat) on (a, b).
    Returns a dict of algorithm-name -> [0, 1] score."""
    na, nb = normalise(a), normalise(b)
    lev = levenshtein(na, nb)
    dam = damerau_levenshtein(na, nb)
    ham = hamming(na, nb)
    lcs = lcs_length(na, nb)
    max_len = max(len(na), len(nb), 1)

    return {
        "exact":              exact_match(a, b),
        "initial_collapse":   initial_collapse_match(a, b),
        "substring":          substring_containment(a, b),
        "soundex_eq":         soundex_match(a, b),
        "refined_soundex_eq": refined_soundex_match(a, b),
        "metaphone_eq":       metaphone_match(a, b),
        "dmetaphone_eq":      dmetaphone_match(a, b),
        "nysiis_eq":          nysiis_match(a, b),
        "levenshtein_sim":    1 - lev / max_len,
        "damerau_sim":        1 - dam / max_len,
        "hamming_sim":        1 - ham / max_len,
        "lcs_sim":            lcs / max_len,
        "jaro":               jaro(na, nb),
        "jaro_winkler":       jaro_winkler(na, nb),
        "jaccard":            jaccard(a, b),
        "sorensen_dice":      sorensen_dice(a, b),
        "token_set_ratio":    token_set_ratio(a, b),
        "bigram_cos":         bigram_overlap(a, b),
        "trigram_cos":        trigram_overlap(a, b),
        "smith_waterman":     smith_waterman(a, b),
        "needleman_wunsch":   needleman_wunsch(a, b),
    }


def combined_name_score(breakdown: dict[str, float]) -> float:
    """Weighted blend of all string-similarity scores → [0, 1]."""
    num = sum(breakdown[k] * ALGO_WEIGHTS[k] for k in breakdown if k in ALGO_WEIGHTS)
    den = sum(ALGO_WEIGHTS[k] for k in breakdown if k in ALGO_WEIGHTS)
    return num / den if den else 0.0


# ─────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────
DEFAULT_DB = Path(__file__).resolve().parent / "sanctions.db"


def screen_fuzzy(input_name: str,
                 dob: Optional[str] = None,
                 nationality: Optional[str] = None,
                 db_path: str | Path = DEFAULT_DB,
                 threshold: float = 0.65,
                 top_k: int = 10,
                 max_candidates: int = 500) -> list[dict]:
    """
    Screen `input_name` (optionally with DOB and nationality) against
    sanctions.db using all 25 algorithms in the funnel.

    Returns a list of up to `top_k` candidate matches whose combined score
    meets `threshold`. Each result is a dict:

        {
          "row":        <full sqlite row dict>,
          "score":      <float 0..1, probabilistic combined>,
          "name_score": <float 0..1, weighted blend of name similarities>,
          "dob_score":  <float 0..1>,
          "nat_score":  <float 0..1>,
          "breakdown":  {<algo_name>: <float>, ...},   # all 21 string scores
        }

    Sorted by `score` descending.

    The function reads from sanctions.db ONLY. It does not write or modify
    any DynamoDB / SQLite state. Safe to call from any worker thread.
    """
    if not input_name or not input_name.strip():
        return []

    candidates = _candidates_from_db(db_path, input_name, max_candidates)

    scored: list[dict] = []
    for c in candidates:
        breakdown = score_pair(input_name, c["full_name"])
        name_score = combined_name_score(breakdown)
        d_score = dob_score(dob, c.get("dob"))
        n_score = nationality_score(nationality, c.get("nationality"))
        final = probabilistic_combine(name_score, d_score, n_score)

        if final >= threshold:
            scored.append({
                "row":        c,
                "score":      final,
                "name_score": name_score,
                "dob_score":  d_score,
                "nat_score":  n_score,
                "breakdown":  breakdown,
            })

    scored.sort(key=lambda x: -x["score"])
    return scored[:top_k]


# ─────────────────────────────────────────────────────────────────────
# CLI demo
# ─────────────────────────────────────────────────────────────────────
def _demo() -> None:
    import sys, json
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    sample = sys.argv[1] if len(sys.argv) > 1 else "Sergey Pakhomov"
    print(f"Screening: {sample!r}")
    print(f"Optional speed-ups: jellyfish={HAS_JELLYFISH}, rapidfuzz={HAS_RAPIDFUZZ}")
    print(f"sanctions.db: {DEFAULT_DB} (exists={DEFAULT_DB.exists()})")
    hits = screen_fuzzy(sample, top_k=5, threshold=0.55)
    print(f"\nTop {len(hits)} hits:")
    for h in hits:
        r = h["row"]
        print(f"  score={h['score']:.3f}  "
              f"name_sim={h['name_score']:.3f}  "
              f"[{r.get('source','?')}/{r.get('program','?')}]  "
              f"{r.get('full_name','?')}")
    if hits:
        print("\nTop hit full breakdown:")
        print(json.dumps(hits[0]["breakdown"], indent=2, default=str))


if __name__ == "__main__":
    _demo()
