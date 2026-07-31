"""
sheet_core.py

Non-GUI logic for finding the best architectural floor-plan sheet inside a
multi-discipline PDF drawing set (permit sets, construction documents, etc.)
so it can be exported and imported into an RF planning tool (Ekahau, iBwave).

Everything here is plain Python + PyMuPDF (fitz) + Pillow (+ optional
Tesseract OCR). No tkinter import, so it can be unit-tested / run headlessly.

Many real-world permit-set PDFs (including the ones this tool was built
against) are produced by a "print to PDF" step that rasterizes all text into
JPEG images while leaving the drawing linework as vector paths. There is no
extractable text layer at all in that case. To handle this, every page first
tries normal vector text extraction; if that comes back essentially empty,
the page is rendered to an image and run through Tesseract OCR instead. The
rest of the classification pipeline (sheet-number regex, keyword scoring,
tag density) works the same either way, against a common list of "lines".
"""

from __future__ import annotations

import io
import os
import re
import sys
import glob
import shutil
from dataclasses import dataclass
from typing import Callable, Optional

import fitz  # PyMuPDF
from PIL import Image

# --------------------------------------------------------------------------
# Optional OCR backend (Tesseract via pytesseract). The tool works without
# it (falls back to "Unknown" for image-only pages) but recognition of
# image-only sheets -- which is common -- needs it.
# --------------------------------------------------------------------------

_OCR_AVAILABLE = False
_pytesseract = None


def _resource_base() -> str:
    """Directory to look for a bundled tesseract/ folder next to the app,
    both when run as a plain script and when frozen by PyInstaller."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def configure_ocr() -> bool:
    """Locate a usable Tesseract install: first a copy bundled alongside
    this app (tesseract/tesseract.exe), then the system install. Returns
    True if OCR is available."""
    global _OCR_AVAILABLE, _pytesseract
    try:
        import pytesseract
    except ImportError:
        _OCR_AVAILABLE = False
        return False

    bundled = os.path.join(_resource_base(), "tesseract", "tesseract.exe")
    bundled_tessdata = os.path.join(_resource_base(), "tesseract", "tessdata")

    if os.path.isfile(bundled):
        pytesseract.pytesseract.tesseract_cmd = bundled
        os.environ["TESSDATA_PREFIX"] = bundled_tessdata
        _pytesseract = pytesseract
        _OCR_AVAILABLE = True
        return True

    exe = shutil.which("tesseract")
    candidates = [exe] if exe else []
    candidates += [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            pytesseract.pytesseract.tesseract_cmd = c
            _pytesseract = pytesseract
            _OCR_AVAILABLE = True
            return True

    _OCR_AVAILABLE = False
    return False


def ocr_available() -> bool:
    return _OCR_AVAILABLE


configure_ocr()

# --------------------------------------------------------------------------
# Discipline prefix map (longest prefixes first so "LS" beats "L", etc.)
# --------------------------------------------------------------------------

_PREFIX_TABLE = [
    ("LS", "Life Safety"),
    ("FP", "Fire Protection"),
    ("FA", "Fire Alarm"),
    ("ID", "Interior Design"),
    ("AD", "Architectural (demo)"),
    ("SD", "Structural (demo)"),
    ("MD", "Mechanical (demo)"),
    ("ED", "Electrical (demo)"),
    ("PD", "Plumbing (demo)"),
    ("CD", "Civil (demo)"),
    ("LA", "Landscape"),
    ("GN", "General"),
    ("A", "Architectural"),
    ("S", "Structural"),
    ("E", "Electrical"),
    ("M", "Mechanical"),
    ("P", "Plumbing"),
    ("C", "Civil"),
    ("L", "Landscape"),
    ("T", "Title / General"),
    ("G", "General"),
    ("D", "Demolition"),
]
DISCIPLINE_BY_PREFIX = dict(_PREFIX_TABLE)

_ALT = "|".join(p for p, _ in _PREFIX_TABLE)
# Trailing suffix allows an optional letter (revision/variant, e.g. "A2.2a")
# optionally followed by one more digit (e.g. "A2.1a0", seen in the wild),
# and/or a dash sub-sheet tag (e.g. "E2.10a-L" / "E2.10a-P" for split
# lighting/power sheets). The numeric part allows up to two dotted segments
# (e.g. "A-1.6.15"). Without this, a confident, large, correct title-block
# match gets rejected and the algorithm falls back to a much smaller, wrong
# one -- or worse, no prefix at all, silently losing the discipline signal
# entirely.
SHEET_NUM_RE = re.compile(
    rf"^({_ALT})[\-\.\s]{{0,2}}(\d{{1,3}}(?:\.\d{{1,2}}){{0,2}})([A-Za-z]{{0,2}}\d{{0,1}}(?:-[A-Za-z]{{1,3}})?)$"
)

# dimension strings like  5' - 6"   or   43'-4"   or   0' - 3 1/2"
_FOOT = "['′’]"
_INCH = '["″”]'
DIM_RE = re.compile(rf"\d{{1,3}}\s*{_FOOT}\s*-?\s*\d{{0,2}}(?:\s+\d/\d)?\s*{_INCH}")

FLOOR_PLAN_HINTS = ("FLOOR PLAN", "1ST FLOOR", "2ND FLOOR", "3RD FLOOR", "4TH FLOOR",
                    "5TH FLOOR", "6TH FLOOR", "GROUND FLOOR")
# "LEVEL 1"/"LEVEL 2" deliberately excluded from the list above -- bare
# "LEVEL N" is too generic and matches incidental mentions in schedules and
# notes (e.g. a lighting schedule's "LEVEL 1 Copy 1" view label, or a note
# saying "...on outdoor spaces of LEVEL 1"), not just genuine floor-plan
# titles.
# Kept deliberately specific/compound. Generic single words like "NOTES",
# "SCHEDULE", "DETAIL" or "KEY PLAN" are excluded on purpose -- a real floor
# plan sheet routinely carries a general-notes block or a small inset key
# plan alongside the actual plan, so matching those bare words anywhere on
# the page produces false exclusions.
EXCLUDE_HINTS = ("SITE PLAN", "ROOF PLAN", "LANDSCAPE PLAN", "EXTERIOR ELEVATION",
                 "BUILDING ELEVATION", "BUILDING SECTION", "WALL SECTION",
                 "COVER SHEET", "RENDERING", "3D VIEW", "PERSPECTIVE", "DEMOLITION PLAN",
                 # Some firms number life-safety / egress / occupancy-calc plans
                 # inside the regular "A" (architectural) series instead of a
                 # separate "LS-" prefix, so the sheet-number prefix alone can't
                 # catch these -- the title says so directly. They're still
                 # floor-plan-shaped, but overlaid with egress arrows, exit
                 # signs, occupancy load counts, etc. cluttering the walls, so
                 # they're a worse RF/3D import source than a clean base plan.
                 # Bare "LIFE SAFETY" also catches legend/notes variants
                 # ("LIFE SAFETY LEGEND") and travel-distance callouts, which
                 # otherwise slip a stray "2ND FLOOR" past the floor-plan check.
                 "LIFE SAFETY", "PLAN OCCUPANCY",
                 # Reflected ceiling plans share plan-view geometry with a
                 # floor plan but are annotated for ceiling features
                 # (lighting, soffits, heights), not a clean base for wall
                 # tracing -- and "RCP" is a distinctive enough acronym in
                 # AEC drawings to be safe as a bare match.
                 "RCP", "REFLECTED CEILING",
                 # Structural slab-edge/pour-strip dimension plans, not
                 # architectural room layout, despite sharing the "A-" prefix
                 # and an incidental "LEVEL N" in the title on some sets.
                 "SLAB DIMENSION")
ENLARGED_HINTS = ("ENLARGED FLOOR PLAN", "ENLARGED UNIT PLAN", "ENLARGED PLAN")

_KEYWORD_SCORES = {
    "Electrical": ["PANEL SCHEDULE", "CIRCUIT", "RECEPTACLE", "CONDUIT", "LIGHTING FIXTURE",
                   "VOLT", "PANELBOARD", "LUMINAIRE"],
    "Mechanical": ["DUCT", "DIFFUSER", "AHU", "CFM", "THERMOSTAT", "HVAC", "CONDENSING UNIT",
                   "RETURN AIR", "SUPPLY AIR"],
    "Plumbing": ["WASTE", "VENT STACK", "SANITARY", "DOMESTIC WATER", "FIXTURE UNIT",
                 "WATER HEATER", "HOSE BIB"],
    "Structural": ["JOIST", "FOOTING", "REBAR", "TRUSS", "SHEAR WALL", "BEAM SCHEDULE",
                   "STRUCTURAL NOTES", "COLUMN SCHEDULE"],
    "Life Safety": ["EGRESS", "OCCUPANT LOAD", "FIRE RATED", "TRAVEL DISTANCE",
                     "SPRINKLER", "FIRE EXTINGUISHER"],
    "Architectural": ["FLOOR PLAN", "DOOR SCHEDULE", "WINDOW SCHEDULE", "WALL TYPE",
                       "ROOM FINISH", "GENERAL NOTES BUILDING"],
}

LOW_TAG_DENSITY = 0.9   # small text spans per sq.in. of page - below this reads as "clean"
HIGH_TAG_DENSITY = 2.2  # above this reads as "cluttered"

# A genuine single floor-plan sheet has one primary view (occasionally two,
# if there's a small inset key/site plan), so one "SCALE:" label -- rarely
# two. A "typical details" sheet crams many unrelated small drawings onto
# one page, each with its own scale note. This catches those sheets
# structurally, which is far more reliable than keyword matching: a details
# sheet's incidental notes ("...AS SHOWN ON FLOOR PLANS") can otherwise slip
# past keyword checks that a real floor-plan-hint match would catch.
SCALE_LABEL_RE = re.compile(r"SCALE\s*:?", re.IGNORECASE)
MULTI_DETAIL_SCALE_COUNT_SOFT = 3   # a few insets -- fine alongside a real floor-plan title
MULTI_DETAIL_SCALE_COUNT_HARD = 8   # this many separate details is never "one floor plan"

# Large buildings often split a floor into one small-scale "Overall" sheet
# plus several enlarged "Section A" / "Section B" ... sheets. This matches
# the lettered-section suffix specifically (not "Building Section" / "Wall
# Section", which are vertical-cut elevation views with no plan-view walls
# at all and are already caught by EXCLUDE_HINTS).
PARTIAL_SECTION_RE = re.compile(r"-\s*SECTION\s+[A-H]\b", re.IGNORECASE)

MULTI_PLAN_PHRASE_COUNT = 4  # "FLOOR PLAN" repeated this many times = several small plans stacked on one sheet

# General notes on any sheet routinely cross-reference other sheets ("SEE
# FLOOR PLANS FOR LOCATIONS", "REFER TO FLOOR PLAN FOR PARTITION TYPE"),
# which otherwise look identical to a genuine floor-plan title match. Strip
# these out before hint-matching. The lead-in word and the hint phrase can
# land in separate wrapped lines of the same sentence (PDF text extraction
# splits on visual line breaks, not sentence structure), so this works on
# the flattened, newline-collapsed text rather than per-line.
_ALL_HINT_ALT = "|".join(re.escape(h) for h in FLOOR_PLAN_HINTS + EXCLUDE_HINTS + ENLARGED_HINTS)
CROSS_REF_RE = re.compile(
    rf"\b(?:SEE|REFER TO|SHOWN ON|SHOWN IN|PER)\b[^.]{{0,60}}?(?:{_ALL_HINT_ALT})",
    re.IGNORECASE,
)


def _strip_cross_refs(text_upper: str) -> str:
    flat = text_upper.replace("\n", " ")
    return CROSS_REF_RE.sub(" ", flat)

OCR_TRIGGER_CHARS = 20   # if vector text has fewer real characters than this, try OCR
OCR_RENDER_DPI = 250
ROTATION_PROBE_DPI = 110


@dataclass
class SheetInfo:
    file_path: str
    page_index: int
    page_count: int
    sheet_number: str
    sheet_title: str
    discipline: str
    is_recommended: bool
    reason: str
    score: int
    dim_count: int
    tag_density: float
    width_pt: float
    height_pt: float
    used_ocr: bool = False
    rotation_fix: int = 0  # clockwise degrees to apply so the sheet renders upright

    @property
    def file_name(self) -> str:
        return os.path.basename(self.file_path)

    @property
    def display_id(self) -> str:
        return self.sheet_number or f"p.{self.page_index + 1}"


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def discover_pdfs(path: str) -> list[str]:
    """Return a sorted list of PDF paths for a single file or a folder."""
    if os.path.isdir(path):
        return sorted(glob.glob(os.path.join(path, "*.pdf")))
    if os.path.isfile(path) and path.lower().endswith(".pdf"):
        return [path]
    return []


# --------------------------------------------------------------------------
# Orientation detection.
#
# Some source PDFs (notably files produced by certain "print to PDF" CAD
# pipelines) have no /Rotate flag at all, but the drawing content itself --
# including any raster title-block text -- is physically drawn sideways or
# upside down within the page. Vector-text pages can be checked precisely
# and instantly from each text line's writing direction. Image-only pages
# have no such data, so as a fallback we render small probes at all four
# candidate rotations and keep whichever one OCRs into the most recognizable
# English words (Tesseract's own orientation detector needs denser prose
# than sparse CAD title-block text and is unreliable here).
# --------------------------------------------------------------------------

def _rotation_from_vector_text(page: "fitz.Page") -> Optional[int]:
    raw = page.get_text("dict")
    dir_counts = {0: 0, 90: 0, 180: 0, 270: 0}
    total = 0
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1, 0))
            total += 1
            if abs(dx) >= abs(dy):
                dir_counts[0 if dx >= 0 else 180] += 1
            else:
                dir_counts[270 if dy > 0 else 90] += 1
    if total < 5:
        return None
    best = max(dir_counts, key=dir_counts.get)
    if dir_counts[best] / total > 0.7:
        return best
    return 0


def _rotation_from_ocr_probe(page: "fitz.Page", dpi: int = ROTATION_PROBE_DPI) -> int:
    if not _OCR_AVAILABLE:
        return 0
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    base_img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")

    from pytesseract import Output
    best_angle, best_score = 0, -1
    for angle in (0, 90, 180, 270):
        test_img = base_img.rotate(-angle, expand=True) if angle else base_img
        try:
            data = _pytesseract.image_to_data(test_img, output_type=Output.DICT, config="--psm 11")
        except Exception:
            continue
        score = 0
        for i, w in enumerate(data["text"]):
            w = w.strip()
            if len(w) >= 3 and w.isalpha():
                try:
                    conf = float(data["conf"][i])
                except (ValueError, TypeError):
                    conf = -1
                if conf > 40:
                    score += 1
        if score > best_score:
            best_score = score
            best_angle = angle
    return best_angle


def detect_rotation(page: "fitz.Page") -> int:
    """Clockwise degrees (0/90/180/270) to rotate a normal render of this
    page so it reads upright."""
    from_text = _rotation_from_vector_text(page)
    if from_text is not None:
        return from_text
    return _rotation_from_ocr_probe(page)


def _apply_rotation(img: Image.Image, rotation_fix: int) -> Image.Image:
    if not rotation_fix:
        return img
    return img.rotate(-rotation_fix, expand=True)


# --------------------------------------------------------------------------
# Unified "lines" extraction: vector text first, OCR fallback
# --------------------------------------------------------------------------

def _lines_from_vector(page: "fitz.Page") -> list[dict]:
    lines = []
    raw = page.get_text("dict")
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            text = "".join(s["text"] for s in spans).strip()
            if not text:
                continue
            size = max((s["size"] for s in spans), default=0.0)
            lines.append({"text": text, "size": size})
    return lines


def _lines_from_ocr(page: "fitz.Page", rotation_fix: int, dpi: int = OCR_RENDER_DPI) -> list[dict]:
    if not _OCR_AVAILABLE:
        return []
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    img = _apply_rotation(img, rotation_fix)

    from pytesseract import Output
    data = _pytesseract.image_to_data(img, output_type=Output.DICT, config="--psm 11")

    grouped: dict[tuple, list[int]] = {}
    n = len(data["text"])
    for i in range(n):
        word = data["text"][i].strip()
        if not word or int(data.get("conf", ["-1"] * n)[i] if isinstance(data["conf"][i], str) else data["conf"][i]) < 0:
            if not word:
                continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        grouped.setdefault(key, []).append(i)

    lines = []
    pt_per_px = 72.0 / dpi
    for key, idxs in grouped.items():
        words = [data["text"][i].strip() for i in idxs if data["text"][i].strip()]
        if not words:
            continue
        text = " ".join(words)
        heights = [data["height"][i] for i in idxs]
        max_height_px = max(heights) if heights else 0
        size_pt = max_height_px * pt_per_px * 1.3  # rough px-height -> pt-size fudge
        lines.append({"text": text, "size": size_pt})
    return lines


CORNER_CROP_W_FRAC = 0.24
CORNER_CROP_H_FRAC = 0.14
CORNER_CROP_DPI = 300


def _sheet_number_from_corner(page: "fitz.Page", rotation_fix: int) -> str:
    """Sheet numbers are, by near-universal AEC drafting convention, the
    largest text in a boxed cell in the bottom-right corner of the title
    block. OCR-ing that small region in isolation is far more reliable than
    hunting for it across a full, cluttered page (seals, logos and decorative
    firm lettering elsewhere on the sheet often OCR as larger/noisier text
    than the actual sheet number)."""
    if not _OCR_AVAILABLE:
        return ""
    scale = CORNER_CROP_DPI / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    img = _apply_rotation(img, rotation_fix)
    w, h = img.size
    crop = img.crop((int(w * (1 - CORNER_CROP_W_FRAC)), int(h * (1 - CORNER_CROP_H_FRAC)), w, h))
    crop = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)

    try:
        text = _pytesseract.image_to_string(crop, config="--psm 11")
    except Exception:
        return ""

    best_num, best_len = "", 0
    for raw_line in text.splitlines():
        compact = raw_line.replace(" ", "").strip()
        if not compact:
            continue
        m = SHEET_NUM_RE.match(compact)
        if m and len(compact) > best_len:
            best_num, best_len = compact.upper(), len(compact)
    return best_num


def _get_lines(page: "fitz.Page", rotation_fix: int) -> tuple[list[dict], bool]:
    lines = _lines_from_vector(page)
    total_chars = sum(len(l["text"]) for l in lines)
    if total_chars >= OCR_TRIGGER_CHARS or not _OCR_AVAILABLE:
        return lines, False
    ocr_lines = _lines_from_ocr(page, rotation_fix)
    if ocr_lines:
        return ocr_lines, True
    return lines, False


# --------------------------------------------------------------------------
# Per-page analysis
# --------------------------------------------------------------------------

def _find_sheet_number_and_title(lines: list[dict]) -> tuple[str, str]:
    best_num, best_size = "", 0.0
    title_candidates: list[tuple[float, str]] = []

    for line in lines:
        line_text = line["text"]
        line_size = line["size"]
        compact = line_text.replace(" ", "")

        m = SHEET_NUM_RE.match(compact)
        if m and line_size > best_size:
            best_size = line_size
            best_num = compact.upper()

        words = line_text.split()
        if len(words) >= 2 and not SHEET_NUM_RE.match(compact) and not line_text[0].isdigit():
            title_candidates.append((line_size, line_text))

    title = ""
    if title_candidates:
        title_candidates.sort(key=lambda t: t[0], reverse=True)
        title = title_candidates[0][1]
    return best_num, title


def _score_keywords(text_upper: str) -> dict[str, int]:
    scores = {}
    for discipline, words in _KEYWORD_SCORES.items():
        hits = sum(text_upper.count(w) for w in words)
        if hits:
            scores[discipline] = hits
    return scores


def _tag_density(lines: list[dict], page_rect) -> float:
    """Count of small text lines (callouts/tags) per square inch -- a
    text-based proxy for 'visual clutter' since reliable CAD-symbol
    recognition needs a trained model. Correlates well with MEP/structural
    sheets, which are dense with tiny reference tags."""
    small = sum(1 for l in lines if l["text"] and l["size"] and l["size"] <= 7.5)
    area_sqin = (page_rect.width / 72.0) * (page_rect.height / 72.0)
    return small / area_sqin if area_sqin else 0.0


def analyze_page(file_path: str, doc: "fitz.Document", page_index: int) -> SheetInfo:
    page = doc[page_index]
    rotation_fix = detect_rotation(page)
    lines, used_ocr = _get_lines(page, rotation_fix)
    text = "\n".join(l["text"] for l in lines)
    text_upper = text.upper()

    fallback_number, sheet_title = _find_sheet_number_and_title(lines)

    if used_ocr:
        # Whole-page OCR text is noisy (seals/logos/stylized firm lettering
        # often OCR as bigger or cleaner-looking than the real sheet number),
        # so for image-only pages trust *only* the isolated corner-box read
        # and leave it blank rather than accept a low-confidence guess.
        sheet_number = _sheet_number_from_corner(page, rotation_fix)
    else:
        sheet_number = fallback_number

    discipline = None
    if sheet_number:
        m = SHEET_NUM_RE.match(sheet_number)
        if m:
            discipline = DISCIPLINE_BY_PREFIX.get(m.group(1))

    kw_scores = _score_keywords(text_upper)
    if discipline is None:
        discipline = max(kw_scores, key=kw_scores.get) if kw_scores else "Unknown"

    dim_count = len(DIM_RE.findall(text))
    tag_density = round(_tag_density(lines, page.rect), 2)

    # Per-line OCR size estimates are too noisy to reliably tell "heading"
    # from "body text" (a solid-block ALL-CAPS notes paragraph can measure
    # taller than the actual view-title line), so hint matching runs against
    # the whole page rather than trying to isolate a single title line. This
    # is safe as long as the hint phrases themselves are specific compound
    # phrases rather than generic single words (see EXCLUDE_HINTS /
    # ENLARGED_HINTS comments), and as long as cross-reference notes ("SEE
    # FLOOR PLAN FOR...") pointing at some *other* sheet are stripped out
    # first -- otherwise those are indistinguishable from a genuine title.
    scan_text_upper = _strip_cross_refs(text_upper)

    is_floor_plan = any(h in scan_text_upper for h in FLOOR_PLAN_HINTS)
    is_excluded = any(h in scan_text_upper for h in EXCLUDE_HINTS)
    is_enlarged = any(h in scan_text_upper for h in ENLARGED_HINTS)
    scale_label_count = len(SCALE_LABEL_RE.findall(text))
    # Two tiers: a genuine large-building floor plan can legitimately carry a
    # few small insets (site key plan, a callout detail) alongside the main
    # view -- that alone shouldn't disqualify it. A sheet with a *lot* of
    # separate scaled details, though, is never a single floor plan, no
    # matter what an incidental keyword match might suggest (e.g. a signage
    # details sheet whose notes happen to say "...as shown on floor plans").
    is_multi_detail_soft = scale_label_count >= MULTI_DETAIL_SCALE_COUNT_SOFT
    is_multi_detail_hard = scale_label_count >= MULTI_DETAIL_SCALE_COUNT_HARD
    # Large buildings often split each floor into one small-scale "Overall"
    # sheet plus several larger-scale "Section A"/"Section B"... sheets
    # covering just part of the floor each. Both are genuine floor-plan
    # content, but the "Overall" sheet is the one that shows the complete
    # floor in a single image, so it's the one to recommend by default.
    is_partial_section = bool(PARTIAL_SECTION_RE.search(scan_text_upper)) and "OVERALL" not in scan_text_upper
    # A sheet with the phrase "FLOOR PLAN" repeated several times over is
    # almost always several small plans stacked on one sheet (e.g. a trash
    # room or elevator lobby shown once per level), not one complete
    # building/floor plan -- even though it technically satisfies the
    # floor-plan keyword check. A genuine single plan says it once (title
    # text is occasionally duplicated by the PDF itself, hence the margin).
    floor_plan_phrase_count = scan_text_upper.count("FLOOR PLAN")
    is_multiple_stacked_plans = floor_plan_phrase_count >= MULTI_PLAN_PHRASE_COUNT

    score = 0
    reason = ""

    if discipline == "Architectural":
        score += 100
        if is_multi_detail_hard:
            score -= 80
            is_excluded = True
            reason = (f"Architectural, but this is a typical-details sheet "
                      f"({scale_label_count} separate details, each at its own scale) — "
                      f"not a single floor plan")
        elif is_multiple_stacked_plans:
            score -= 80
            is_excluded = True
            reason = (f"Architectural, but this sheet stacks {floor_plan_phrase_count} separate small "
                      f"plans (e.g. one room repeated per level) rather than one complete floor plan")
        elif is_floor_plan and not is_excluded:
            score += 30
            if is_partial_section:
                score -= 25
                reason = ("Enlarged partial section of a larger floor plan — "
                          "look for the matching \"Overall\" sheet for the complete floor")
            else:
                reason = "Full wall/room layout"
            if is_multi_detail_soft:
                reason += f", plus {scale_label_count} smaller inset details on the same sheet"
        elif is_excluded:
            score -= 60
            hit = next((h for h in EXCLUDE_HINTS if h in scan_text_upper), "non-floor-plan content")
            reason = f"Architectural, but appears to be a {hit.title()}, not a floor plan"
        elif is_multi_detail_soft:
            score -= 50
            is_excluded = True
            reason = (f"Architectural, but looks like a details sheet "
                      f"({scale_label_count} separate details, no clear floor-plan title) — "
                      f"not a single floor plan")
        else:
            reason = "Architectural sheet"

        if is_enlarged:
            score -= 15
            reason += " (enlarged / partial plan)"

        if dim_count >= 6:
            score += 15
            reason += f", fully dimensioned ({dim_count} dimension strings)"
        elif dim_count == 0:
            reason += ", no dimension strings found"

        if tag_density <= LOW_TAG_DENSITY:
            score += 10
            reason += ", minimal callout clutter"
        elif tag_density >= HIGH_TAG_DENSITY:
            score -= 10
            reason += ", heavy callout/tag clutter"

    elif discipline == "Unknown":
        if not lines:
            reason = "No readable text found (image-only page and OCR unavailable) — check manually"
        else:
            reason = "Could not confidently classify this sheet — check manually"
    else:
        reason = f"{discipline} sheet"
        if discipline in ("Electrical", "Mechanical", "Plumbing"):
            reason += " — MEP overlay, not base wall geometry"
        elif discipline == "Structural":
            reason += " — framing/joist layout, not finished wall partitions"
        elif discipline == "Life Safety":
            reason += " — egress/occupancy overlay on an architectural base"
        elif discipline == "Fire Protection":
            reason += " — sprinkler/standpipe layout"

    is_recommended = (discipline == "Architectural" and is_floor_plan
                       and not is_excluded and not is_partial_section)

    if not sheet_title:
        sheet_title = "(untitled)"
    if used_ocr:
        reason += "  [read via OCR — no text layer in source PDF]"

    return SheetInfo(
        file_path=file_path,
        page_index=page_index,
        page_count=doc.page_count,
        sheet_number=sheet_number,
        sheet_title=sheet_title,
        discipline=discipline,
        is_recommended=is_recommended,
        reason=reason.strip(", "),
        score=score,
        dim_count=dim_count,
        tag_density=tag_density,
        width_pt=page.rect.width,
        height_pt=page.rect.height,
        used_ocr=used_ocr,
        rotation_fix=rotation_fix,
    )


# --------------------------------------------------------------------------
# Top-level scan
# --------------------------------------------------------------------------

ProgressCB = Optional[Callable[[int, int, str], None]]


def scan_pdf(file_path: str, progress_cb: ProgressCB = None) -> list[SheetInfo]:
    results: list[SheetInfo] = []
    doc = fitz.open(file_path)
    try:
        total = doc.page_count
        for i in range(total):
            if progress_cb:
                progress_cb(i, total, f"Scanning {os.path.basename(file_path)} — page {i + 1}/{total}")
            results.append(analyze_page(file_path, doc, i))
    finally:
        doc.close()
    return results


def scan_source(path: str, progress_cb: ProgressCB = None) -> list[SheetInfo]:
    pdfs = discover_pdfs(path)
    all_results: list[SheetInfo] = []
    for pdf in pdfs:
        all_results.extend(scan_pdf(pdf, progress_cb))
    return all_results


# --------------------------------------------------------------------------
# Rendering / export
# --------------------------------------------------------------------------

def render_thumbnail(file_path: str, page_index: int, max_dim: int = 900,
                      rotation_fix: int = 0) -> Image.Image:
    doc = fitz.open(file_path)
    try:
        page = doc[page_index]
        scale = max_dim / max(page.rect.width, page.rect.height)
        scale = max(scale, 0.1)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        img.load()
        return _apply_rotation(img, rotation_fix)
    finally:
        doc.close()


def _safe_stub(sheet: SheetInfo) -> str:
    base = os.path.splitext(sheet.file_name)[0]
    num = sheet.sheet_number or f"p{sheet.page_index + 1}"
    stub = f"{base}_{num}"
    return re.sub(r'[\\/:*?"<>|]', "-", stub)


def export_sheets(
    sheets: list[SheetInfo],
    out_dir: str,
    dpi: int = 300,
    export_png: bool = True,
    export_pdf: bool = True,
    progress_cb: ProgressCB = None,
) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    total = len(sheets)

    by_file: dict[str, list[SheetInfo]] = {}
    for s in sheets:
        by_file.setdefault(s.file_path, []).append(s)

    done = 0
    for file_path, group in by_file.items():
        doc = fitz.open(file_path)
        try:
            for s in group:
                if progress_cb:
                    progress_cb(done, total, f"Exporting {s.display_id} ({s.file_name})")
                stub = _safe_stub(s)
                page = doc[s.page_index]

                if export_png:
                    scale = dpi / 72.0
                    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    png_path = os.path.join(out_dir, f"{stub}.png")
                    if s.rotation_fix:
                        img = Image.open(io.BytesIO(pix.tobytes("png")))
                        img = _apply_rotation(img, s.rotation_fix)
                        img.save(png_path, dpi=(dpi, dpi))
                    else:
                        pix.save(png_path)
                    written.append(png_path)

                if export_pdf:
                    single = fitz.open()
                    single.insert_pdf(doc, from_page=s.page_index, to_page=s.page_index)
                    if s.rotation_fix:
                        # the source page has no /Rotate flag but is drawn
                        # sideways/upside-down -- stamp the correct /Rotate
                        # so the exported single-page PDF opens upright.
                        single[0].set_rotation(s.rotation_fix)
                    pdf_path = os.path.join(out_dir, f"{stub}.pdf")
                    single.save(pdf_path)
                    single.close()
                    written.append(pdf_path)

                done += 1
        finally:
            doc.close()

    if progress_cb:
        progress_cb(total, total, "Export complete")
    return written
