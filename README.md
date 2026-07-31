# Floor Plan Sheet Selector/Extractor

A small Windows desktop tool that scans a multi-discipline architectural PDF
drawing set (permit sets, construction documents) and finds the sheet(s)
that show the **complete architectural floor plan** — full interior and
exterior wall geometry — as opposed to the electrical, mechanical,
structural, plumbing, life-safety, site, roof, elevation, or detail sheets
mixed in with it. The result exports as a high-resolution PNG and/or a
single-page PDF, ready to import into an RF planning tool (Ekahau, iBwave)
or a 3D modeling tool.

Built with Python's `tkinter` (GUI) and [PyMuPDF](https://pymupdf.readthedocs.io/)
(PDF reading, rendering, export), with a bundled [Tesseract](https://github.com/tesseract-ocr/tesseract)
OCR fallback for PDFs that store their text as pictures instead of real text.

## Features

- **Select PDF File** or **Select Folder of PDFs** to scan a single drawing
  set or batch-process every PDF in a folder
- Automatic per-page scanning and classification by discipline (sheet-number
  prefix, title-block text, and structural signals like dimension-string and
  scale-label density)
- Results list showing every sheet found, with recommended sheet(s)
  highlighted and a one-line reason for the call
- Live preview pane — click any row to see that sheet before exporting
- Click a different sheet at any time to override the recommendation
- Export the selected sheet(s) as 300+ DPI PNG and/or single-page PDF
- Clear status messages throughout ("Scanning...", "Done — 3 sheets found",
  "Exported successfully to ...")

## Installation / Running

### Option 1 — Just run the app (recommended)

Double-click:

```
dist\FloorPlanSheetPicker.exe
```

No Python install needed — it's fully standalone. The first launch is a few
seconds slower than normal while it unpacks itself to a temp folder; that's
expected for a single-file `.exe` this size (it has a full OCR engine built
in).

### Option 2 — Run from source

Use this if the `.exe` has any issues, or you want to modify the code.

1. Install Python 3.10+ from [python.org](https://python.org) if you don't
   already have it.
2. From this folder, install dependencies and run:

   ```powershell
   pip install -r requirements.txt
   python sheet_picker_app.py
   ```

Keep the `tesseract\` folder alongside the scripts — that's what gives both
the `.exe` and this source fallback the ability to read PDFs with no real
text layer. It isn't required for normal PDFs that already have embedded
text.

## Example usage

1. Launch the app and click **Select PDF File...**, then choose a permit set
   PDF (e.g. `Luxe Building Type One Apartments_Permit Set.pdf`).
2. The status bar shows `Scanning...` while it reads every page. When it's
   done, something like:

   ```
   Done — 5 recommended floor plan sheets found across 1 file (388 pages scanned).
   ```

3. The sheet list fills in, with rows like `A2.1A0`, `A2.1B0`, ... highlighted
   amber and marked **★ Recommended**. Clicking a row shows its reason, e.g.:

   ```
   Full wall/room layout, plus 4 smaller inset details on the same sheet,
   fully dimensioned (20 dimension strings), minimal callout clutter
   ```

4. The preview pane on the right renders that page so you can visually
   confirm it before exporting — it's the actual page image, correctly
   oriented even if the source PDF has no rotation flag set.
5. If a different sheet looks better for your needs (e.g. you want an
   enlarged unit plan instead of the whole-building overall), just click it
   in the list — the preview updates immediately.
6. Click **Export Selected Sheet(s)** (or **Export All Recommended** to grab
   every starred sheet at once), pick a save format (PNG / PDF / both) and a
   DPI, then choose a destination folder. Status bar confirms:

   ```
   Exported successfully to C:\Users\you\Documents\Exports
   ```

7. Import the exported PNG or PDF into your RF planning tool at the DPI you
   exported with, and calibrate scale using the sheet's printed dimension
   strings.

## How it decides which sheet is "the" floor plan

Each page's sheet number (`A-`, `S-`, `E-`, `M-`, `P-`, `LS-`, `FP-`, ...) is
read from its title block to identify the discipline. Architectural sheets
are then checked for genuine floor-plan content — full wall/room layout,
real dimension strings — versus:

- Site plans, roof plans, elevations, sections, and detail sheets (excluded
  even though they're technically "architectural" too)
- Life-safety, occupancy, and reflected-ceiling-plan (RCP) sheets, even when
  a firm numbers them inside the regular architectural series instead of a
  separate discipline prefix
- Sheets that stack several small unrelated plans or details on one page
  (e.g. a "typical details" sheet, or a small utility room repeated once per
  level) rather than showing one complete floor plan
- Cross-reference notes ("see floor plan for partition type") that mention a
  floor plan without actually being one

For PDFs with no embedded text layer, an OCR fallback reads the sheet number
directly from the title-block corner and auto-detects page orientation
(useful for files with no `/Rotate` flag where the content is physically
drawn sideways).

The reasoning for every sheet is always shown in the app — if a
recommendation looks wrong, click a different sheet to override it. OCR-based
classification in particular is best-effort, not perfect, which is exactly
why the override is one click away.

## Project structure

| File | Purpose |
|---|---|
| `sheet_core.py` | Scanning, classification, and export logic (no GUI dependency — can be imported and tested headlessly) |
| `sheet_picker_app.py` | The tkinter desktop app |
| `requirements.txt` | Python package dependencies |
| `tesseract\` | Bundled OCR engine (English), used as a fallback for PDFs with no embedded text layer |
| `dist\FloorPlanSheetPicker.exe` | The packaged standalone app |
| `FloorPlanSheetPicker.spec` | PyInstaller build recipe |

## Rebuilding the .exe

```powershell
pip install pyinstaller
pyinstaller FloorPlanSheetPicker.spec --clean --noconfirm
```

The output lands in `dist\FloorPlanSheetPicker.exe`.

## Known limitations

- Tuned against US, English-language, imperial-dimensioned AEC drawing
  conventions — non-US sheet numbering, metric dimensioning, or
  non-English text will weaken some of the signals it relies on.
- OCR-based classification (for image-only PDFs) is best-effort; always
  check the preview before exporting.
- Firm-specific title-block phrasing that doesn't match any of the known
  patterns may need a new keyword added to `sheet_core.py` — treat any
  wrong recommendation as a bug report.
