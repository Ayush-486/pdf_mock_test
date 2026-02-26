"""
PDF to Mock Test - FastAPI Backend
Robust state-machine MCQ parser for JEE/NEET style PDFs.
"""

import os
import re
import sqlite3
import tempfile

import pdfplumber
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ──────────────────────────────────────────────
# App setup
# ──────────────────────────────────────────────

app = FastAPI(title="PDF to Mock Test")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_PATH = os.path.join(BASE_DIR, "questions.db")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ──────────────────────────────────────────────
# Database helpers
# ──────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create (or recreate) the questions table."""
    with get_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS questions")
        conn.execute(
            """
            CREATE TABLE questions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                question    TEXT    NOT NULL,
                option_a    TEXT,
                option_b    TEXT,
                option_c    TEXT,
                option_d    TEXT,
                has_diagram INTEGER DEFAULT 0
            )
            """
        )
        conn.commit()


def insert_questions(questions: list[dict]):
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO questions (question, option_a, option_b, option_c, option_d, has_diagram)
            VALUES (:question, :option_a, :option_b, :option_c, :option_d, :has_diagram)
            """,
            questions,
        )
        conn.commit()


# ──────────────────────────────────────────────
# Regex patterns
# ──────────────────────────────────────────────

# Matches all question number formats:
#   1.   1)   1:   Q1   Q1.   Q.1   Q 1   Question 1   Que 1   Q1)
# Captures: (number, optional_rest_of_question_text)
QUESTION_RE = re.compile(
    r"""
    ^                               # start of line
    (?:
        (?:Question|Que)\.?\s+      # "Question 1" or "Que 1"
      | Q\.?\s*                     # "Q1" / "Q.1" / "Q 1"
    )?
    (\d+)                           # question number
    \s*                             # optional space
    [.):\u2013\-]?                  # optional delimiter: . ) : – -
    \s*                             # optional trailing space
    (.*)                            # rest of line (may be empty)
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Matches option lines:
#   A)  A.  A:  A-  (A)  [A]  a)  a.  (a)
# Captures: (letter, option_text)
OPTION_RE = re.compile(
    r"""
    ^
    [\(\[]?                         # optional open bracket
    ([A-Da-d])                      # option letter A-D
    [\)\].]                         # closing bracket or dot
    \s*[-:]?\s*                     # optional dash/colon separator
    (.+)                            # option text (must have content)
    $
    """,
    re.VERBOSE,
)

# Sections that signal end of questions
STOP_PATTERNS = re.compile(
    r"^\s*(?:answer\s*key|answer\s*sheet|solutions?|explanations?|hints?)\b",
    re.IGNORECASE,
)

# Lines that are purely noise / headers (page numbers, section titles with no digits)
NOISE_RE = re.compile(
    r"^\s*(?:page\s*\d+|\d+\s*/\s*\d+|www\.|http|©|copyright)\s*$",
    re.IGNORECASE,
)

# Standalone number line (table rows / data lines):  "6.67 × 10⁻¹¹", "0.12", etc.
# We want to detect a line that is ONLY a question number and nothing else,
# meaning the question text is on the NEXT line.
QNUM_ONLY_RE = re.compile(
    r"""
    ^
    (?:(?:Question|Que)\.?\s+|Q\.?\s*)?   # optional Q prefix
    (\d+)                                  # number
    \s*[.):\-]?\s*                         # optional delimiter
    $                                      # nothing after
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _is_valid_question_start(match: re.Match, line: str) -> bool:
    """
    Extra heuristic: if the QUESTION_RE matched but the number is suspiciously
    large (> 200) or the rest looks like data (pure float), treat it as noise.
    """
    num = int(match.group(1))
    rest = match.group(2).strip()
    if num > 200:
        return False
    # If the first token of 'rest' is a standalone number/fraction, might be data
    # But do NOT reject here as the question text may start with a number.
    return True


def _option_letter_to_key(letter: str) -> str:
    return f"option_{letter.lower()}"


# ──────────────────────────────────────────────
# State-machine parser
# ──────────────────────────────────────────────

def parse_questions_from_text(full_text: str) -> list[dict]:
    """
    State-machine MCQ parser.
    States: IDLE → IN_QUESTION → IN_OPTIONS
    Returns list of dicts: {question, option_a, option_b, option_c, option_d, has_diagram}
    """
    questions: list[dict] = []

    # State
    current_q: dict | None = None
    state = "IDLE"          # IDLE | IN_QUESTION | IN_OPTIONS
    stopped = False         # True after hitting answer key/solution section

    # We track page-level diagram info separately and merge later
    # (diagram info is attached during PDF extraction, not here)

    def _finish_question():
        """Validate and save current question."""
        nonlocal current_q
        if current_q is None:
            return
        num_opts = sum(
            1 for k in ("option_a", "option_b", "option_c", "option_d")
            if current_q.get(k)
        )
        if num_opts >= 2:
            questions.append(current_q)
        current_q = None

    lines = full_text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        i += 1

        if not line:
            continue

        # ── Hard stop on section headers ────────────────────────────────
        if STOP_PATTERNS.match(line):
            _finish_question()
            stopped = True

        if stopped:
            continue

        # ── Skip obvious noise ───────────────────────────────────────────
        if NOISE_RE.match(line):
            continue

        # ── Check for option first (higher priority than question in IN_OPTIONS)
        opt_match = OPTION_RE.match(line)

        # ── Check for question-number-only line ──────────────────────────
        qnum_only = QNUM_ONLY_RE.match(line)

        # ── Check for full question line  ────────────────────────────────
        q_match = QUESTION_RE.match(line)

        # Validate question match with heuristics
        if q_match and not _is_valid_question_start(q_match, line):
            q_match = None

        # Suppress question match if we're collecting options and the match
        # looks like it might be an option-continuation or stray number.
        if state == "IN_OPTIONS" and q_match:
            num = int(q_match.group(1))
            # If there's an active question and this looks like just a number
            # in text (e.g., "12 m/s"), don't treat as new question.
            # Only accept as new question if the number makes sense sequentially.
            if questions and num <= int(questions[-1].get("_num", 0)):
                q_match = None

        # ─────────────────────────────────────────────────────────────────
        # Transitions
        # ─────────────────────────────────────────────────────────────────

        if qnum_only:
            # Number-only line: start a new question, text on next line(s)
            _finish_question()
            num = int(qnum_only.group(1))
            current_q = {
                "question": "",
                "option_a": None, "option_b": None,
                "option_c": None, "option_d": None,
                "has_diagram": 0,
                "_num": num,
            }
            state = "IN_QUESTION"

        elif q_match and q_match.group(2).strip():
            # Question number + text on same line
            _finish_question()
            num = int(q_match.group(1))
            text = q_match.group(2).strip()
            current_q = {
                "question": text,
                "option_a": None, "option_b": None,
                "option_c": None, "option_d": None,
                "has_diagram": 0,
                "_num": num,
            }
            state = "IN_QUESTION"

        elif opt_match and current_q is not None:
            # Option line
            letter = opt_match.group(1).lower()
            text = opt_match.group(2).strip()
            key = _option_letter_to_key(letter)
            # Only set if we haven't already (first occurrence wins)
            if key in current_q and not current_q[key]:
                current_q[key] = text
            state = "IN_OPTIONS"

        elif current_q is not None:
            if state == "IN_QUESTION":
                # Continuation of question text (options not yet seen)
                sep = " " if current_q["question"] else ""
                current_q["question"] += sep + line

            elif state == "IN_OPTIONS":
                # Could be continuation of the last option (multi-line option text)
                # Find the last set option and append
                for key in ("option_d", "option_c", "option_b", "option_a"):
                    if current_q.get(key):
                        current_q[key] += " " + line
                        break

    # Flush last question
    _finish_question()

    # Clean up internal keys
    for q in questions:
        q.pop("_num", None)

    return questions


# ──────────────────────────────────────────────
# PDF extraction (page-by-page with diagram detection)
# ──────────────────────────────────────────────

def extract_text_from_pdf(pdf_path: str) -> tuple[str, set[int]]:
    """
    Extract text page by page; skip pages that fail.
    Returns:
        (full_text, pages_with_images)
    """
    all_text: list[str] = []
    pages_with_images: set[int] = set()

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            try:
                text = page.extract_text()
                if text:
                    all_text.append(text)
                # Detect images / figures on this page
                if page.images:
                    pages_with_images.add(page_num)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] Skipping page {page_num}: {exc}")

    return "\n".join(all_text), pages_with_images


def parse_with_diagram_info(pdf_path: str) -> list[dict]:
    """
    Full pipeline: extract text + detect diagram pages, parse MCQs,
    and heuristically mark questions that likely have diagrams based on
    surrounding page context.
    """
    full_text, diagram_pages = extract_text_from_pdf(pdf_path)
    questions = parse_questions_from_text(full_text)

    # If the PDF has any diagram pages, we mark all questions as potentially
    # having a diagram (since we can't easily map question → exact page after
    # plain text extraction).  We flag it if ANY page had images.
    # A more refined approach would need per-page question parsing.
    has_any_diagram = 1 if diagram_pages else 0
    for q in questions:
        if has_any_diagram:
            q["has_diagram"] = 1  # Conservative: mark all if images found

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

    # Save to a temp file
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
                "Make sure the PDF contains standard question numbering "
                "(1. / Q1. / Q.1 / Question 1) and option labels (A) B) C) D))."
            ),
        )

    init_db()
    insert_questions(questions)

    return JSONResponse({"count": len(questions), "redirect": "/test"})


@app.get("/api/questions")
async def get_all_questions():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, question, option_a, option_b, option_c, option_d, has_diagram "
            "FROM questions ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]
