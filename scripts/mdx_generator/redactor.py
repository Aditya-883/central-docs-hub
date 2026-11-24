"""Blur sensitive data in page screenshots, using local OCR (Tesseract).

What gets blurred is defined by the same patterns your `preprocessor.py` already uses to mask
text (so the screenshot and the text are always treated the same way):

    EMAIL, PHONE, SSN, CAGE code, UEI code, Primary NAICS, Client Name

Two sources are combined:
  1. Patterns   - the regexes from preprocessor.py are run over the text OCR finds in the image.
  2. Real values - the actual emails / UEI / CAGE / ... found in the project's JSON files are
                   searched for in the image, even if they are not written next to a label there.
                   (Values are collected from ALL pages of a portal, so something that is
                   typed on the Users page is also blurred on the Notifications page.)

Nothing leaves the machine: OCR and blurring are local. The blurred copy is what is saved to
static/img and what is sent to Gemini.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Iterable

from .config import logger
from .preprocessor import (
    CAGE_LABELED_RE,
    CLIENT_NAME_LABELED_RE,
    EMAIL_RE,
    PHONE_RE,
    PRIMARY_NAICS_LABELED_RE,
    SSN_RE,
    UEI_LABELED_RE,
    _redaction_token_for_key,
)

# (regex, group to blur). Group 0 = the whole match, group 2 = only the value after the label.
PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (EMAIL_RE, 0),
    (PHONE_RE, 0),
    (SSN_RE, 0),
    (CAGE_LABELED_RE, 2),
    (UEI_LABELED_RE, 2),
    (PRIMARY_NAICS_LABELED_RE, 2),
    (CLIENT_NAME_LABELED_RE, 2),
]
# A UEI is 12 letters+digits. Also catches one printed without a label right next to it.
UEI_SHAPED_RE = re.compile(r"^\(?(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{12}\)?[,.]?$")

# Optional: person names (the name printed right above an e-mail). Off unless REDACT_PERSON_NAMES=1.
NAME_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z.'\- ]{1,38}$")

os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # one Tesseract thread per image; we run several images in parallel instead

# Set OCR_THOROUGH=1 for slower 5-pass 3x-scale OCR; default 2-pass 2x-scale is ~3-4x faster.
OCR_THOROUGH = os.getenv("OCR_THOROUGH", "0") == "1"
OCR_SCALE = int(os.getenv("OCR_SCALE", "3" if OCR_THOROUGH else "2"))
PAD = 4             # extra pixels around every blurred box
FUZZY_MATCH = 0.85  # tolerance for OCR typos when matching known values

Box = tuple[int, int, int, int]


# --------------------------------------------------------------------------- OCR availability
def _configure_tesseract() -> None:
    import pytesseract

    cmd = os.getenv("TESSERACT_CMD")
    if not cmd and not shutil.which("tesseract"):
        for candidate in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            "/opt/homebrew/bin/tesseract",
            "/usr/local/bin/tesseract",
        ):
            if Path(candidate).exists():
                cmd = candidate
                break
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd


def ocr_available() -> bool:
    try:
        import pytesseract
        from PIL import Image  # noqa: F401

        _configure_tesseract()
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- what to blur
def _walk(obj: Any, key: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(obj, str):
        yield key, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, str(k))
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, key)


def extract_pii_values(raw: Any, include_person_names: bool = False) -> set[str]:
    """Collect the actual sensitive values from ONE original (unmasked) page JSON."""
    found: set[str] = set()
    labels: set[str] = set()
    if include_person_names and isinstance(raw, dict):
        for t in raw.get("tables") or []:
            labels.update(h.strip().lower() for h in (t.get("headers") or []) if isinstance(h, str))
        for h in raw.get("headings") or []:
            labels.add((h.get("text", "") if isinstance(h, dict) else str(h)).strip().lower())

    for key, text in _walk(raw):
        text = text.strip()
        if not text:
            continue
        # Values stored under keys like "uei", "cageCode", "clientName", "primaryNaics"
        if _redaction_token_for_key(key) and len(text) <= 120:
            found.add(text)
        for rx, group in PATTERNS:
            for m in rx.finditer(text):
                value = (m.group(group) or "").strip()
                if value:
                    found.add(value)
        for m in PRIMARY_NAICS_LABELED_RE.finditer(text):
            code = re.match(r"\d{6}", m.group(2))
            if code:
                found.add(code.group(0))
        if include_person_names:
            lines = [ln.strip() for ln in text.splitlines()]
            for i, ln in enumerate(lines):
                if i and (EMAIL_RE.fullmatch(ln) or ln == "[REDACTED_EMAIL]"):
                    prev = lines[i - 1]
                    if NAME_LINE_RE.match(prev) and 1 <= len(prev.split()) <= 4 and prev.lower() not in labels:
                        found.add(prev)
    return found


def collect_pii_terms(json_paths: Iterable[Path], extra_terms: Iterable[str] = (), include_person_names: bool = False) -> list[str]:
    """Read the ORIGINAL json files (the pipeline's own loader masks PII first, which would hide the values)."""
    terms: set[str] = {t.strip() for t in extra_terms if t and t.strip()}
    for path in json_paths:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        terms |= extract_pii_values(raw, include_person_names)
    return sorted(terms)


# --------------------------------------------------------------------------- OCR + matching
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9@._-]", "", s.lower())


def _similar(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a) and bool(b) and (a == b or difflib.SequenceMatcher(None, a, b).ratio() >= FUZZY_MATCH)


def _union(boxes: list[Box]) -> Box:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _ocr_lines(img, psm: int) -> list[list[tuple[str, Box]]]:
    """Run Tesseract once; return recognised words (text + box) grouped by text line."""
    import pytesseract
    from PIL import Image

    _configure_tesseract()
    big = img.resize((img.width * OCR_SCALE, img.height * OCR_SCALE), Image.LANCZOS)
    data = pytesseract.image_to_data(big, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
    lines: dict[tuple[int, int, int], list[tuple[str, Box]]] = {}
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        if not text or float(data["conf"][i]) < 0:
            continue
        x, y, w, h = (data[k][i] // OCR_SCALE for k in ("left", "top", "width", "height"))
        lines.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(
            (text, (x, y, x + w, y + h))
        )
    return list(lines.values())


def _find_boxes(img, terms: list[str]) -> list[Box]:
    from PIL import ImageOps

    rgb = img.convert("RGB")
    blue = rgb.split()[2]  # light text on a blue banner reads cleanly in the blue channel
    # Default: 2 fast passes covering light and blue-banner layouts. Set OCR_THOROUGH=1 for all 5 passes.
    passes = (
        [(rgb, 3), (rgb, 11), (ImageOps.invert(rgb), 11), (blue, 11), (blue, 6)]
        if OCR_THOROUGH else
        [(rgb, 11), (blue, 11)]
    )
    term_tokens = [t.split() for t in terms if t.strip()]
    boxes: list[Box] = []

    for image, psm in passes:
        for words in _ocr_lines(image, psm):
            # Build the line's text so patterns that span several words (phone numbers,
            # "UEI: XXXXXXXXXXXX", "Primary NAICS: 541512 - ...") can be matched.
            offsets: list[tuple[int, int]] = []
            pos = 0
            for text, _ in words:
                offsets.append((pos, pos + len(text)))
                pos += len(text) + 1
            line = " ".join(t for t, _ in words)

            def span_box(start: int, end: int) -> Box | None:
                hit = [words[i][1] for i, (a, b) in enumerate(offsets) if a < end and b > start]
                return _union(hit) if hit else None

            for rx, group in PATTERNS:
                for m in rx.finditer(line):
                    if m.group(group):
                        b = span_box(*m.span(group))
                        if b:
                            boxes.append(b)
            for text, box in words:
                if UEI_SHAPED_RE.match(text):
                    boxes.append(box)

            # Known values taken from the project's JSON
            for tokens in term_tokens:
                n = len(tokens)
                for s in range(len(words) - n + 1):
                    if all(_similar(words[s + j][0], tokens[j]) for j in range(n)):
                        boxes.append(_union([words[s + j][1] for j in range(n)]))
    return boxes


def redact_image(src: Path, dest: Path, terms: list[str]) -> int:
    """Write a blurred copy of `src` to `dest`. Returns the number of regions blurred."""
    from PIL import Image, ImageFilter

    img = Image.open(src)
    fmt = img.format or "PNG"
    work = img.convert("RGB")
    boxes = _find_boxes(work, terms)
    for x0, y0, x1, y1 in boxes:
        box = (max(0, x0 - PAD), max(0, y0 - PAD), min(work.width, x1 + PAD), min(work.height, y1 + PAD))
        region = work.crop(box)
        work.paste(region.filter(ImageFilter.GaussianBlur(max(8, box[3] - box[1]))), box)
    import tempfile

    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(dest.parent), delete=False, prefix=".tmp_img_", suffix=dest.suffix or ".png") as tmp:
        tmp_path = Path(tmp.name)
    try:
        work.save(tmp_path, format=fmt)
        os.replace(tmp_path, dest)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    logger.info("Blurred %d region(s) in %s", len(boxes), dest.name)
    return len(boxes)