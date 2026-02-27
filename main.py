"""
PDF to Mock Test – FastAPI Backend
Coordinate-aware spatial MCQ parser for JEE/NEET and all real-world PDF formats.

PDF extraction uses page.extract_words(use_text_flow=True, keep_blank_chars=True)
so every word retains its (x0, top, bottom) coordinates.  Words are grouped into
visual lines by their `top` coordinate, preserving per-word x0 for indentation
analysis.  The state-machine parser operates on these spatial lines.

Image extraction saves embedded images to /static/images/ and attaches each
image to the question whose vertical Y-range is closest (±80 px tolerance).

Supported question styles:
  1.  1)  (1)  1:  01.  Q1  Q1.  Q.1  Q 1  Que 1  Question 1
  I.  II.  III.  IV.  V.  VI.  VII.  VIII.  IX.  X.
  OCR-spaced: "2 1 2" → question 212

Supported option styles:
  A)  A.  A:  (A)  [A]  a)  (a)
  (i) (ii) (iii) (iv)
  1)  1.  2)  2.  3)  3.  4)  4.
  • bullet   * star   - dash   – en-dash
"""

import os
import re
import sqlite3
import tempfile
import uuid

import pdfplumber
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ──────────────────────────────────────────────
# App setup
# ──────────────────────────────────────────────

app = FastAPI(title="PDF to Mock Test")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
IMAGES_DIR = os.path.join(STATIC_DIR, "images")
DB_PATH    = os.path.join(BASE_DIR, "questions.db")

os.makedirs(IMAGES_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ──────────────────────────────────────────────
# Database helpers
# ──────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create (or recreate) the questions table with image_path support."""
    with get_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS questions")
        conn.execute(
            """
            CREATE TABLE questions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                question         TEXT    NOT NULL,
                option_a         TEXT,
                option_b         TEXT,
                option_c         TEXT,
                option_d         TEXT,
                option_a_image   TEXT,
                option_b_image   TEXT,
                option_c_image   TEXT,
                option_d_image   TEXT,
                has_diagram      INTEGER DEFAULT 0,
                image_path       TEXT,
                question_image   TEXT
            )
            """
        )
        conn.commit()


def insert_questions(questions: list[dict]):
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO questions
                (question, option_a, option_b, option_c, option_d,
                 option_a_image, option_b_image, option_c_image, option_d_image,
                 has_diagram, image_path, question_image)
            VALUES
                (:question, :option_a, :option_b, :option_c, :option_d,
                 :option_a_image, :option_b_image, :option_c_image, :option_d_image,
                 :has_diagram, :image_path, :question_image)
            """,
            questions,
        )
        conn.commit()


# ──────────────────────────────────────────────
# Regex patterns  (applied to .strip()-ed line text)
# ──────────────────────────────────────────────

# ── Numeric question with Q-prefix: Q1 Q1. Q.1 Q 1 Que 1 Question 1
# Always valid \u2014 the prefix is an unambiguous signal.
QUESTION_PREFIXED_RE = re.compile(
    r"""
    ^\s*
    (?:
        (?:Question|Que)\.?\s+
      | Q\.?\s*
    )
    \(?
    (0?\d{1,3})
    \)?
    \s*
    [.):\u2013\-]?
    \s*
    (.*)
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

# ── Numeric question WITHOUT prefix: 1. 1) (1) 1:
# REQUIRES a separator (. ) :) after the number \u2014 bare "2 should be..."
# must NOT match (it's a math continuation line).
QUESTION_BARE_NUM_RE = re.compile(
    r"""
    ^\s*
    \(?
    (0?\d{1,3})
    \)?
    \s*
    [.):\u2013\-]       # separator is MANDATORY
    \s*
    (.*)
    $
    """,
    re.VERBOSE,
)

# OCR-spaced question number, e.g. "2 1 2" → "212"
QUESTION_OCR_SPACED_RE = re.compile(
    r"""
    ^\s*
    (\d(?:\s+\d){1,3})   # digits separated by spaces, 2-4 digits total
    \s*[.):\-]?\s*
    (.+)                  # must have question text after
    $
    """,
    re.VERBOSE,
)

# Roman numeral question: I. II. III. IV. V. …
QUESTION_ROMAN_RE = re.compile(
    r"""
    ^\s*
    (
        X{0,3}
        (?:IX|IV|V?I{0,3})
    )
    (?!\()
    \s*
    [.:]
    \s*
    (.*)
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Number-only line (question number alone on its own line)
# REQUIRES a Q./Que/Question prefix — bare numbers like "1 2" or "0" from
# math subscripts/superscripts must NOT be treated as question headers.
QNUM_ONLY_RE = re.compile(
    r"""
    ^\s*
    (?:(?:Question|Que)\.?\s+|Q\.\s*)
    \(?
    (0?\d{1,3})
    \)?
    \s*[.):\-]?\s*
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Roman numeral number-only line
QNUM_ROMAN_ONLY_RE = re.compile(
    r"""
    ^\s*
    (X{0,3}(?:IX|IV|V?I{0,3}))
    (?!\()
    \s*[.:]?\s*
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

# ── Option patterns ──────────────────────────────────────────────────────

# Standard letter options: A) A. A: (A) [A] a) (a)
# Allows empty text after label for diagram-reference options like "(A)" alone
OPTION_LETTER_RE = re.compile(
    r"""
    ^\s*
    [\(\[]?
    ([A-Da-d])
    [\)\].:]
    \s*[-:]?\s*
    (.*)
    $
    """,
    re.VERBOSE,
)

# Roman numeral options: (i) (ii) (iii) (iv)
OPTION_ROMAN_RE = re.compile(
    r"""
    ^\s*
    \(
    (i{1,3}|iv|v?i{0,3})
    \)
    \s*
    (.+)
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Numeric options: 1) 2) 3) 4) or 1. 2. 3. 4. (only when inside a question)
OPTION_NUMERIC_RE = re.compile(
    r"""
    ^\s*
    ([1-4])
    [).]
    \s+
    (.+)
    $
    """,
    re.VERBOSE,
)

# Bullet/dash options: • text  * text  - text  – text
OPTION_BULLET_RE = re.compile(
    r"""
    ^\s*
    [•\*\-–]
    \s+
    (.+)
    $
    """,
    re.VERBOSE,
)

# Stop / noise patterns
STOP_PATTERNS = re.compile(
    r"""
    ^\s*
    (?:
        answers?\s*(?:[&]|and)\s*solutions?   # "Answers & Solutions" / "and Solutions"
      | answer\s*key                           # "Answer Key"
      | answer\s*sheet
      | solutions?                             # "Solution" / "Solutions"
      | explanations?                          # "Explanation" / "Explanations"
      | hints?
    )
    \b
    """,
    re.VERBOSE | re.IGNORECASE,
)

NOISE_RE = re.compile(
    r"^\s*(?:page\s*\d+|\d+\s*/\s*\d+|www\.|http|©|copyright)\s*$",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────
# Helper utilities
# ──────────────────────────────────────────────

def _is_valid_question_start(num_str: str) -> bool:
    try:
        return int(num_str.lstrip("0") or "0") <= 200
    except ValueError:
        return True


def _option_letter_to_key(letter: str) -> str:
    return f"option_{letter.lower()}"


def _roman_option_to_key(roman: str) -> str | None:
    mapping = {"i": "a", "ii": "b", "iii": "c", "iv": "d"}
    k = mapping.get(roman.lower())
    return f"option_{k}" if k else None


def _numeric_to_option_key(digit: str) -> str | None:
    mapping = {"1": "a", "2": "b", "3": "c", "4": "d"}
    k = mapping.get(digit)
    return f"option_{k}" if k else None


def _try_option(line: str, in_question: bool) -> tuple[str | None, str | None]:
    """Try to match line as any option format."""
    m = OPTION_LETTER_RE.match(line)
    if m:
        return _option_letter_to_key(m.group(1)), m.group(2).strip()
    if in_question:
        m = OPTION_ROMAN_RE.match(line)
        if m:
            key = _roman_option_to_key(m.group(1))
            if key:
                return key, m.group(2).strip()
        m = OPTION_NUMERIC_RE.match(line)
        if m:
            key = _numeric_to_option_key(m.group(1))
            if key:
                return key, m.group(2).strip()
        m = OPTION_BULLET_RE.match(line)
        if m:
            return "__bullet__", m.group(1).strip()
    return None, None


def _assign_bullet_option(current_q: dict, text: str) -> str | None:
    for key in ("option_a", "option_b", "option_c", "option_d"):
        if not current_q.get(key):
            current_q[key] = text
            return key
    return None


def _count_options(q: dict) -> int:
    return sum(1 for k in ("option_a", "option_b", "option_c", "option_d") if q.get(k))


def _looks_math_fragment(text: str) -> bool:
    t = text.strip()
    if not t or len(t) > 28:
        return False
    return re.fullmatch(r"[A-Za-z0-9\[\]\(\)\+\-−=*/.:\s]+", t) is not None


# ── Math character normalization ──────────────────────────────────────────────
# Some PDFs (especially Indian exam PDFs) use Symbol or custom math fonts where
# glyph codes don't match the expected Unicode codepoints.  This table maps the
# most common problematic encodings to their correct Unicode equivalents.
_MATH_CHAR_MAP: dict[str, str] = {
    "\uf028": "√",   # radical sign in some Symbol-variant fonts
    "\uf0d6": "√",   # alternative radical encoding
    "\uf0b0": "°",   # degree in Symbol font
    "\uf0b2": "²",   # superscript 2 in some fonts
    "\uf0b3": "³",   # superscript 3
    "\uf02d": "−",   # minus in Symbol font
    "\u221a": "√",   # U+221A – already correct, normalise to same char
    "\u2212": "−",   # U+2212 minus – already correct
}


def _normalize_math_chars(text: str) -> str:
    """Map known symbol-font mis-encodings to correct Unicode math characters."""
    return "".join(_MATH_CHAR_MAP.get(c, c) for c in text)


def _append_option_text(existing: str | None, incoming: str) -> str:
    """
    Merge split option fragments while preserving math layout as plain text.

    Handles common OCR/PDF splits like:
      - `t[1+e` + `]` + `1−e`  -> `t[1+e] / 1−e`
      - `mv2` + `0` + `x2` + `0` -> `mv20 / x20`
    """
    new_part = incoming.strip()
    if not new_part:
        return (existing or "").strip()

    current = (existing or "").rstrip()
    if not current:
        return new_part

    if new_part in {"]", ")"}:
        return current + new_part

    compact_current = re.sub(r"\s+", "", current)
    compact_new = re.sub(r"\s+", "", new_part)

    # Subscript/exponent continuation like `mv2` + `0` -> `mv20`
    if re.fullmatch(r"\d+", compact_new) and compact_current and compact_current[-1].isalnum():
        return current + compact_new

    starts_like_denominator = (
        compact_new.lower().startswith("x")
        or re.match(r"^\d*x\d", compact_new, re.IGNORECASE) is not None
        or re.match(r"^\d+[+\-−][A-Za-z0-9]+$", compact_new) is not None
    )
    current_looks_like_numerator = (
        current.endswith("]")
        or "mv" in compact_current.lower()
        or re.search(r"[+\-−]", compact_current) is not None
    )

    if (
        "/" not in current
        and starts_like_denominator
        and current_looks_like_numerator
        and _looks_math_fragment(current)
        and _looks_math_fragment(new_part)
    ):
        return f"{current} / {new_part}"

    return f"{current} {new_part}"


def _normalize_math_option_text(text: str | None) -> str | None:
    if text is None:
        return None
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized or "/" in normalized:
        return normalized

    # Compact OCR fragments from stacked fractions in mechanics PDFs.
    # Examples:
    #   mv20x20      -> mv20 / x20
    #   mv202x20     -> mv20 / 2x20
    #   3 mv202 x20  -> 3 mv20 / 2 x20
    normalized = re.sub(
        r"(?i)\b(mv2\s*0)\s*([23]?\s*x2\s*0)\b",
        r"\1 / \2",
        normalized,
    )
    return normalized


# ──────────────────────────────────────────────
# Text extraction: extract_text() + chars for Y-coordinates
# ──────────────────────────────────────────────
# Many JEE/NEET PDFs encode individual characters as separate glyphs with
# wide inter-character spacing.  pdfplumber's extract_words() treats each
# char as a separate "word" regardless of x_tolerance.
#
# However, extract_text() uses pdfplumber's internal layout engine which
# correctly reconstructs words with proper spacing.  So we:
#   1. Use extract_text() to get properly-spaced text lines.
#   2. Use page.chars to build a Y-coordinate lookup for each line.
#   3. Combine them into the same visual-line dicts the parser expects.
# ──────────────────────────────────────────────

# Indentation tolerance: if a continuation line's x0 is at least this many
# points to the right of the option label's x0, treat it as continuation text.
INDENT_TOL = 10.0

# Tolerance for matching chars to a text line's Y-coordinate.
LINE_Y_TOL = 5.0


def _extract_text_lines(page) -> list[dict]:
    """
    Build visual lines directly from raw character data.

    Groups chars by Y-position (LINE_Y_TOL tolerance), reconstructs text
    by inserting spaces where there is a horizontal gap between chars, and
    merges subscript/superscript rows (avg font-size < 80% of dominant)
    into the preceding line.

    This approach avoids the index-alignment bug that occurred when pairing
    extract_text() lines with y_rows derived from chars — pdfplumber's
    extract_text() folds subscripts inline while the char data keeps them on
    separate Y-rows, causing a one-off mismatch for every subscript line.

    Returns list of dicts sorted by vertical position:
        [{"text": str, "top": float, "bottom": float, "x0": float}, …]
    """
    chars = page.chars or []
    if not chars:
        return []

    # Sort chars by top Y then x0
    sorted_chars = sorted(chars, key=lambda c: (float(c["top"]), float(c.get("x0", 0))))

    # Group into Y-rows by top tolerance
    rows: list[list] = []
    current_row = [sorted_chars[0]]
    current_top = float(sorted_chars[0]["top"])
    for c in sorted_chars[1:]:
        c_top = float(c["top"])
        if abs(c_top - current_top) <= LINE_Y_TOL:
            current_row.append(c)
        else:
            rows.append(current_row)
            current_row = [c]
            current_top = c_top
    if current_row:
        rows.append(current_row)

    # Dominant font size (median across all chars)
    all_sizes = sorted(
        float(c.get("size", 0))
        for row in rows
        for c in row
        if float(c.get("size", 0)) > 0
    )
    dominant_size = all_sizes[len(all_sizes) // 2] if all_sizes else 12.0

    result: list[dict] = []
    for row in rows:
        row_sorted = sorted(row, key=lambda c: float(c.get("x0", 0)))

        # Reconstruct text, inserting a space wherever char gap > 25% of font size
        text_parts: list[str] = []
        prev_x1: float | None = None
        for c in row_sorted:
            ch = c.get("text", "")
            if not ch:
                continue
            x0 = float(c.get("x0", 0))
            sz = float(c.get("size", dominant_size)) or dominant_size
            x1 = float(c.get("x1", x0 + sz * 0.5))
            if prev_x1 is not None and x0 - prev_x1 > sz * 0.25:
                text_parts.append(" ")
            text_parts.append(ch)
            prev_x1 = max(prev_x1 or 0.0, x1)

        text = "".join(text_parts).strip()
        if not text:
            continue

        # Normalise symbol-font mis-encodings (e.g. \uf028 → √)
        text = _normalize_math_chars(text)

        avg_top  = sum(float(c["top"]) for c in row) / len(row)
        avg_bot  = sum(float(c.get("bottom", c["top"] + 12)) for c in row) / len(row)
        min_x0   = min(float(c.get("x0", 0)) for c in row)
        sizes_row = [float(c.get("size", 0)) for c in row if float(c.get("size", 0)) > 0]
        avg_size  = sum(sizes_row) / len(sizes_row) if sizes_row else 0.0

        is_sub = avg_size > 0 and avg_size < dominant_size * 0.80

        if is_sub and result:
            prev = result[-1]
            prev_center = (prev["top"] + prev["bottom"]) / 2.0
            row_center  = avg_top + (avg_bot - avg_top) / 2.0
            # Row center above previous line center → superscript; else subscript
            if row_center < prev_center:
                sup_map = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
                text = text.translate(sup_map)
            else:
                sub_map = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")
                text = text.translate(sub_map)
            result[-1]["text"] += text
            result[-1]["bottom"] = max(result[-1]["bottom"], avg_bot)
        else:
            result.append({
                "text":   text,
                "top":    avg_top,
                "bottom": avg_bot,
                "x0":     min_x0,
            })

    return result


# ──────────────────────────────────────────────
# State-machine parser (operates on visual lines)
# ──────────────────────────────────────────────

def parse_questions_from_lines(visual_lines: list[dict]) -> list[dict]:
    """
    Coordinate-aware state-machine MCQ parser that works on spatially-grouped
    visual lines.

    Each entry in `visual_lines` is:
        {"text": str, "top": float, "bottom": float, "x0": float}

    States: IDLE → IN_QUESTION → IN_OPTIONS

    Returns list of dicts:
        {question, option_a…option_d, has_diagram, image_path,
         _num, _y_start, _y_end}

    _y_start / _y_end are the vertical Y-range of the question block
    (used for image attachment, stripped before DB insert).

    CRITICAL: When a new question header appears, the previous question is
    finalized UNCONDITIONALLY — even if options are incomplete.  Two questions
    are NEVER merged.
    """
    questions: list[dict] = []
    current_q: dict | None = None
    state = "IDLE"
    stopped = False
    last_option_key: str | None = None
    last_option_x0: float = 0.0    # x0 of the most recently matched option label

    def _make_question(num_str: str, text: str, y_top: float) -> dict:
        return {
            "question":      text,
            "option_a":      None,
            "option_b":      None,
            "option_c":      None,
            "option_d":      None,
            "option_a_image": None,
            "option_b_image": None,
            "option_c_image": None,
            "option_d_image": None,
            "has_diagram":   0,
            "image_path":    None,
            "_num":          num_str,
            "_y_start":      y_top,
            "_y_end":        y_top,
            "_opt_y":        {},   # {letter: y_top} recorded when each option first appears
        }

    def _finish_question():
        """Finalize current question — always emitted, never gated on option count."""
        nonlocal current_q, last_option_key, last_option_x0
        if current_q is None:
            return
        for key in ("option_a", "option_b", "option_c", "option_d"):
            current_q[key] = _normalize_math_option_text(current_q.get(key))
        # Always emit the question (even with 0 options)
        questions.append(current_q)
        current_q = None
        last_option_key = None
        last_option_x0 = 0.0

    for vl in visual_lines:
        line = vl["text"].strip()
        y_top = vl["top"]
        y_bot = vl["bottom"]
        line_x0 = vl.get("x0", 0.0)

        if not line:
            continue

        # ── Hard stop ─────────────────────────────────────────────────────────
        if STOP_PATTERNS.match(line):
            _finish_question()
            stopped = True
        # Also stop when any line *contains* "solution:" (answer-section marker)
        if not stopped and re.search(r"\bsolution\s*:", line, re.IGNORECASE):
            _finish_question()
            stopped = True
        if stopped:
            continue

        # ── Noise ─────────────────────────────────────────────────────────────
        if NOISE_RE.match(line):
            continue

        # Track Y-extent of current question block
        if current_q is not None:
            current_q["_y_end"] = y_bot

        in_question_ctx = (state in ("IN_QUESTION", "IN_OPTIONS")) and current_q is not None

        # ── 1. Roman numeral question (highest priority, any state) ───────────
        q_rom_match  = QUESTION_ROMAN_RE.match(line)
        qrom_only    = QNUM_ROMAN_ONLY_RE.match(line)

        if q_rom_match and q_rom_match.group(1):
            _finish_question()
            current_q = _make_question(
                q_rom_match.group(1).upper(),
                q_rom_match.group(2).strip(),
                y_top
            )
            state = "IN_QUESTION"
            last_option_key = None
            last_option_x0 = 0.0
            continue

        if qrom_only and qrom_only.group(1):
            _finish_question()
            current_q = _make_question(qrom_only.group(1).upper(), "", y_top)
            state = "IN_QUESTION"
            last_option_key = None
            last_option_x0 = 0.0
            continue

        # ── 2. Try option ──────────────────────────────────────────────────────
        opt_key, opt_text = _try_option(line, in_question=in_question_ctx)

        # ── 3. Numeric question-number-only line ──────────────────────────────
        qnum_only = QNUM_ONLY_RE.match(line)

        # ── 4. Full numeric question line ──────────────────────────────────────
        # Try prefixed pattern first (Q.1, Que 1, Question 1), then bare (1. 1) (1))
        q_num_match = QUESTION_PREFIXED_RE.match(line)
        q_num_has_prefix = bool(q_num_match)

        if not q_num_match:
            q_num_match = QUESTION_BARE_NUM_RE.match(line)

        if q_num_match:
            num_str = q_num_match.group(1)
            rest = q_num_match.group(2).strip()

            if not _is_valid_question_start(num_str):
                q_num_match = None

            # If in options state and no body text, probably a numeric option
            if q_num_match and state == "IN_OPTIONS" and current_q is not None:
                if not rest:
                    q_num_match = None
            # If line also matches as an option, prefer the option interpretation
            if q_num_match and opt_key:
                q_num_match = None

            # Suppress if the body text is an answer/solution reference
            # e.g. "Q.1 Answer: (B)" or "Q1 Solution: ..." must NOT become questions
            if q_num_match and re.search(r"\b(?:answer|solution)\b", rest, re.IGNORECASE):
                q_num_match = None

        # ── 5. OCR-spaced question number: "2 1 2" ────────────────────────────
        # ONLY match in IDLE state — inside a question, spaced digits are almost
        # always math subscripts / superscripts (e.g. m₁ m₂ rendering as "1 2").
        q_ocr_match = None
        if state == "IDLE" and not q_num_match and not opt_key and not qnum_only:
            q_ocr_match = QUESTION_OCR_SPACED_RE.match(line)
            if q_ocr_match:
                collapsed = q_ocr_match.group(1).replace(" ", "")
                if not _is_valid_question_start(collapsed):
                    q_ocr_match = None

        # ── Transitions ───────────────────────────────────────────────────────

        if qnum_only and not opt_key:
            # Question number on its own line
            _finish_question()
            current_q = _make_question(qnum_only.group(1), "", y_top)
            state = "IN_QUESTION"
            last_option_key = None
            last_option_x0 = 0.0

        elif q_num_match and q_num_match.group(2).strip():
            # Full numeric question line with body text
            _finish_question()
            current_q = _make_question(
                q_num_match.group(1),
                q_num_match.group(2).strip(),
                y_top
            )
            state = "IN_QUESTION"
            last_option_key = None
            last_option_x0 = 0.0

        elif q_ocr_match:
            # OCR-spaced question number with body text
            collapsed = q_ocr_match.group(1).replace(" ", "")
            _finish_question()
            current_q = _make_question(
                collapsed,
                q_ocr_match.group(2).strip(),
                y_top
            )
            state = "IN_QUESTION"
            last_option_key = None
            last_option_x0 = 0.0

        elif opt_key and current_q is not None:
            # Option line
            if opt_key == "__bullet__":
                assigned = _assign_bullet_option(current_q, opt_text)
                if assigned:
                    last_option_key = assigned
                    last_option_x0 = line_x0
                    letter = assigned[-1]  # 'a', 'b', 'c', 'd'
                    if letter not in current_q["_opt_y"]:
                        current_q["_opt_y"][letter] = y_top
            else:
                if not current_q.get(opt_key):
                    current_q[opt_key] = opt_text
                    last_option_key = opt_key
                    last_option_x0 = line_x0
                    letter = opt_key[-1]  # 'a', 'b', 'c', 'd'
                    if letter not in current_q["_opt_y"]:
                        current_q["_opt_y"][letter] = y_top
            state = "IN_OPTIONS"

        elif current_q is not None:
            # Continuation line (not a new question, not an option)
            if state == "IN_QUESTION":
                # Multi-line question body
                sep = " " if current_q["question"] else ""
                current_q["question"] += sep + line
            elif state == "IN_OPTIONS":
                # Multi-line option continuation:
                # If current line is indented further than the option label,
                # append to the last option; otherwise append to question text.
                if (last_option_key
                        and current_q.get(last_option_key) is not None
                        and line_x0 >= last_option_x0 + INDENT_TOL):
                    current_q[last_option_key] = _append_option_text(current_q[last_option_key], line)
                elif last_option_key and current_q.get(last_option_key) is not None:
                    # Same or less indentation — still append to last option
                    # (common for wrapped option text at same indent level)
                    current_q[last_option_key] = _append_option_text(current_q[last_option_key], line)

    _finish_question()
    return questions


# Legacy wrapper: parse from raw text (for unit tests / backward compat)
def parse_questions_from_text(full_text: str) -> list[dict]:
    """Parse MCQs from plain text (no spatial info). Lines get synthetic Y coords."""
    fake_lines = []
    for i, raw in enumerate(full_text.splitlines()):
        fake_lines.append({"text": raw, "top": float(i), "bottom": float(i + 1), "x0": 0.0})
    questions = parse_questions_from_lines(fake_lines)
    for q in questions:
        q.pop("_num", None)
        q.pop("_y_start", None)
        q.pop("_y_end", None)
    return questions


# ──────────────────────────────────────────────
# Image extraction helpers
# ──────────────────────────────────────────────

def _save_page_images(page, page_num: int) -> list[dict]:
    """
    Extract diagrams from a pdfplumber page.

    Two sources are tried:
      1. page.images  – embedded raster images (JPEG/PNG streams inside the PDF).
      2. page.figures – bounding boxes of vector-graphic regions (lines, curves,
                        fills drawn with PDF path operators).  This captures
                        diagrams that were drawn rather than embedded, including
                        diagrams that span two pages (each half is captured
                        separately and both halves attach to the same question
                        via the y_offset coordinate system in the caller).

    Saves each region as PNG under static/images/.
    Returns list of dicts: [{"path": web_path, "top": y_top, "bottom": y_bot}, …]
    """
    saved: list[dict] = []
    MIN_DIM = 40.0   # PDF points (~56 px at 96 dpi); ignore tiny decorative elements

    try:
        from PIL import Image as _PILImage  # type: ignore  # noqa: PLC0415, F841
    except ImportError:
        print("[WARN] Pillow not installed – image extraction skipped.")
        return saved

    # ── 1. Embedded raster images ────────────────────────────────────────────
    for idx, img_meta in enumerate(page.images or []):
        try:
            x0 = float(img_meta.get("x0", 0))
            y0 = float(img_meta.get("top", img_meta.get("y0", 0)))
            x1 = float(img_meta.get("x1", page.width))
            y1 = float(img_meta.get("bottom", img_meta.get("y1", page.height)))

            # pdfplumber's page.images uses top-origin coords (top < bottom).
            top    = min(y0, y1)
            bottom = max(y0, y1)
            if top >= bottom or x0 >= x1:
                continue

            # Skip tiny decorative images (logos, favicons, footer strips)
            if (x1 - x0) < MIN_DIM or (bottom - top) < MIN_DIM:
                continue

            cropped = page.crop((x0, top, x1, bottom))
            pil_img = cropped.to_image(resolution=150).original

            fname    = f"page{page_num}_img{idx}_{uuid.uuid4().hex[:6]}.png"
            out_path = os.path.join(IMAGES_DIR, fname)
            pil_img.save(out_path, "PNG")
            saved.append({"path": f"/static/images/{fname}", "top": top, "bottom": bottom})
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Could not extract image on page {page_num} idx {idx}: {exc}")

    # ── 2. Vector/drawn figures via page.figures ─────────────────────────────
    # page.figures groups all path-based graphical objects (rects, lines, curves)
    # into bounding-box regions.  This captures diagrams that are drawn rather
    # than embedded, including those that span two pages.
    for idx2, fig in enumerate(getattr(page, "figures", None) or []):
        try:
            fx0     = float(fig.get("x0", 0))
            ftop    = float(fig.get("top", 0))
            fx1     = float(fig.get("x1", page.width))
            fbottom = float(fig.get("bottom", page.height))

            if (fx1 - fx0) < MIN_DIM or (fbottom - ftop) < MIN_DIM:
                continue

            # Skip if a raster image already covers approximately the same region
            fig_center_y = (ftop + fbottom) / 2.0
            already_covered = any(
                abs(((r["top"] + r["bottom"]) / 2.0) - fig_center_y) < 30
                for r in saved
            )
            if already_covered:
                continue

            cropped = page.crop((fx0, ftop, fx1, fbottom))
            pil_img = cropped.to_image(resolution=150).original

            fname    = f"page{page_num}_fig{idx2}_{uuid.uuid4().hex[:6]}.png"
            out_path = os.path.join(IMAGES_DIR, fname)
            pil_img.save(out_path, "PNG")
            saved.append({"path": f"/static/images/{fname}", "top": ftop, "bottom": fbottom})
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Could not render figure on page {page_num} idx2 {idx2}: {exc}")

    return saved


# ──────────────────────────────────────────────
# Question screenshot helper
# ──────────────────────────────────────────────

def _crop_question_screenshot(
    page_meta: list[dict],
    y_start_global: float,
    y_end_global: float,
    q_idx: int,
) -> str | None:
    """
    Crop the full vertical span of a question, stitching across page boundaries.

    For every page whose visible area overlaps [y_start_global - PAD_TOP, y_end_global]:
      - Convert the overlap to page-local coordinates.
      - Crop full page width for that slice and render at 150 dpi.

    Slices are stitched vertically (top → bottom) into one PIL image and saved.

    Only a small top padding (PAD_TOP) is applied; no padding is added below so
    the next question never bleeds into the screenshot.

    Returns web path /static/images/… or None on failure.
    """
    try:
        from PIL import Image as _PILImage  # type: ignore  # noqa: PLC0415
    except ImportError:
        return None

    PAD_TOP = 6.0  # PDF points of padding above the first line

    slices: list = []  # PIL images ordered top → bottom

    for pm in page_meta:
        page_global_start = pm["y_offset"]
        page_global_end   = pm["y_offset"] + pm["height"]

        # Overlap of the question's global range with this page's global range
        overlap_start = max(y_start_global - PAD_TOP, page_global_start)
        overlap_end   = min(y_end_global,              page_global_end)

        if overlap_end <= overlap_start:
            continue  # this page doesn't contribute

        # Convert overlap to page-local coordinates
        local_start = max(0.0, overlap_start - page_global_start)
        local_end   = min(float(pm["height"]), overlap_end - page_global_start)

        if local_end <= local_start:
            continue

        try:
            cropped = pm["page"].crop((0, local_start, pm["page"].width, local_end))
            slices.append(cropped.to_image(resolution=150).original)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Crop failed Q{q_idx} page {pm['page_num']}: {exc}")

    if not slices:
        return None

    # Stitch slices vertically
    if len(slices) == 1:
        final_img = slices[0]
    else:
        from PIL import Image as _PILImage  # type: ignore  # noqa: PLC0415
        total_h = sum(img.height for img in slices)
        max_w   = max(img.width  for img in slices)
        final_img = _PILImage.new("RGB", (max_w, total_h), color=(255, 255, 255))
        y_cur = 0
        for img in slices:
            final_img.paste(img, (0, y_cur))
            y_cur += img.height

    try:
        fname    = f"qshot{q_idx}_{uuid.uuid4().hex[:6]}.png"
        out_path = os.path.join(IMAGES_DIR, fname)
        final_img.save(out_path, "PNG")
        return f"/static/images/{fname}"
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Save failed Q{q_idx}: {exc}")
        return None


# ──────────────────────────────────────────────
# PDF extraction: spatial word-based pipeline
# ──────────────────────────────────────────────

# Y-axis tolerance (in PDF points) for attaching an image to a question.
IMAGE_Y_TOLERANCE = 150.0


def parse_with_diagram_info(pdf_path: str) -> list[dict]:
    """
    Full spatial pipeline:
      1. For each page, extract text lines (with Y-coords) and embedded images.
      2. Collect a page_meta list so page objects stay accessible while PDF is open.
      3. Parse MCQs from the accumulated visual lines.
      4. Attach embedded images to questions by Y-position.
      5. Promote per-option images.
      6. Crop each question's vertical span as a PNG (question_image).
      7. Clean up internal metadata fields.

    All processing is done inside the pdfplumber.open() 'with' block so that
    page objects remain valid when _crop_question_screenshot() needs them.
    """
    all_visual_lines: list[dict] = []
    all_images: list[dict] = []
    page_meta: list[dict] = []   # {page, y_offset, height, page_num}
    y_offset = 0.0

    with pdfplumber.open(pdf_path) as pdf:
        # ── Pass 1: collect text lines, images, and page metadata ───────────
        for page_num, page in enumerate(pdf.pages, start=1):
            page_y_start = y_offset

            try:
                page_lines = _extract_text_lines(page)
                if page_lines:
                    for pl in page_lines:
                        pl["top"]    += y_offset
                        pl["bottom"] += y_offset
                    all_visual_lines.extend(page_lines)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] Skipping text on page {page_num}: {exc}")

            try:
                page_imgs = _save_page_images(page, page_num)
                for img in page_imgs:
                    img["top"]    += y_offset
                    img["bottom"] += y_offset
                all_images.extend(page_imgs)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] Image extraction failed on page {page_num}: {exc}")

            page_meta.append({
                "page":     page,
                "y_offset": page_y_start,
                "height":   float(page.height),
                "page_num": page_num,
            })
            y_offset += float(page.height) + 20.0

        # ── Parse questions (inside 'with' so page objects remain live) ──────
        questions = parse_questions_from_lines(all_visual_lines)

        # Build a coord-lookup so the second pass can find image positions by path
        path_to_coords: dict[str, tuple[float, float]] = {
            img["path"]: (img["top"], img["bottom"]) for img in all_images
        }

        # ── First pass: attach embedded images to questions by Y-position ────
        for img in all_images:
            img_center_y = (img["top"] + img["bottom"]) / 2.0
            best_q = None
            best_dist = float("inf")

            for q in questions:
                y_start = q.get("_y_start", 0)
                y_end   = q.get("_y_end", 0)
                range_top    = y_start - IMAGE_Y_TOLERANCE
                range_bottom = y_end   + IMAGE_Y_TOLERANCE
                if range_top <= img_center_y <= range_bottom:
                    if y_start <= img_center_y <= y_end:
                        dist = 0.0
                    else:
                        dist = min(abs(img_center_y - y_start), abs(img_center_y - y_end))
                    if dist < best_dist:
                        best_dist = dist
                        best_q = q

            if best_q is None:
                for q in questions:
                    y_start = q.get("_y_start", 0)
                    y_end   = q.get("_y_end", 0)
                    dist = min(abs(img_center_y - y_start), abs(img_center_y - y_end))
                    if dist < best_dist:
                        best_dist = dist
                        best_q = q

            if best_q is not None:
                best_q["has_diagram"] = 1
                if best_q["image_path"] is None:
                    best_q["image_path"] = img["path"]
                else:
                    best_q["image_path"] += "," + img["path"]

        # ── Second pass: promote question-level images to per-option images ──
        for q in questions:
            opt_y: dict[str, float] = q.get("_opt_y", {})
            if not opt_y or not q.get("image_path"):
                continue

            letters_sorted = sorted(opt_y.keys())
            opt_ranges: dict[str, tuple[float, float]] = {}
            for i, letter in enumerate(letters_sorted):
                y_s = opt_y[letter]
                if i + 1 < len(letters_sorted):
                    y_e = opt_y[letters_sorted[i + 1]]
                else:
                    y_e = q.get("_y_end", y_s) + IMAGE_Y_TOLERANCE
                opt_ranges[letter] = (y_s, y_e)

            paths = q["image_path"].split(",")
            remaining: list[str] = []
            for path in paths:
                coords = path_to_coords.get(path)
                if coords is None:
                    remaining.append(path)
                    continue
                cy = (coords[0] + coords[1]) / 2.0
                matched_letter: str | None = None
                for letter, (y_s, y_e) in opt_ranges.items():
                    if y_s - 20 <= cy <= y_e:
                        matched_letter = letter
                        break
                if matched_letter:
                    opt_img_key = f"option_{matched_letter}_image"
                    if not q.get(opt_img_key):
                        q[opt_img_key] = path
                        continue
                remaining.append(path)

            q["image_path"] = ",".join(remaining) if remaining else None

        # ── Screenshot pass: crop each question's region as a PNG ───────────
        for idx, q in enumerate(questions):
            q_y_start = q.get("_y_start", 0.0)
            q_y_end   = q.get("_y_end",   0.0)

            # Clamp bottom edge to next question's start so it never appears
            # in this question's screenshot.
            if idx + 1 < len(questions):
                next_y_start = questions[idx + 1].get("_y_start", q_y_end)
                q_y_end = min(q_y_end, next_y_start)

            q["question_image"] = _crop_question_screenshot(
                page_meta, q_y_start, q_y_end, idx + 1
            )

        # ── Clean up internal metadata ────────────────────────────────────────
        for q in questions:
            q.pop("_num", None)
            q.pop("_y_start", None)
            q.pop("_y_end", None)
            q.pop("_opt_y", None)

    return questions


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/test", response_class=HTMLResponse)
async def serve_test():
    with open(os.path.join(STATIC_DIR, "test.html"), encoding="utf-8") as f:
        return f.read()


@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        questions = parse_with_diagram_info(tmp_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF read error: {exc}") from exc
    finally:
        os.unlink(tmp_path)

    if not questions:
        raise HTTPException(
            status_code=422,
            detail=(
                "No MCQ questions detected. "
                "Ensure the PDF contains standard question numbering "
                "(1. / Q1 / Q.1 / Question 1 / Que 1 / I. II. III. …) "
                "and option labels (A) B) C) D) or (A) a. • etc)."
            ),
        )

    init_db()
    insert_questions(questions)

    return JSONResponse({"count": len(questions), "redirect": "/test"})


@app.get("/api/questions")
async def get_all_questions():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, question, option_a, option_b, option_c, option_d, "
            "option_a_image, option_b_image, option_c_image, option_d_image, "
            "has_diagram, image_path, question_image FROM questions ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# Local dev entry-point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
