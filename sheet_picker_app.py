"""
Floor Plan Sheet Picker

A small desktop tool that scans a multi-discipline architectural PDF (or a
folder of them), figures out which page is the real architectural floor
plan -- as opposed to the electrical, mechanical, structural, plumbing,
site, roof, elevation, or detail sheets mixed in with it -- and exports that
page as a high-resolution image or single-page PDF ready to import into an
RF planning tool (Ekahau, iBwave) or a 3D modeling tool.

Run directly with:  python sheet_picker_app.py
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
import webbrowser
from dataclasses import replace

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk

import sheet_core as sc

APP_TITLE = "Floor Plan Sheet Picker"

DISCIPLINE_COLORS = {
    "Architectural": "#1D6F5C",
    "Structural": "#7A5230",
    "Electrical": "#B5860B",
    "Mechanical": "#2D5F8A",
    "Plumbing": "#4A6FA5",
    "Life Safety": "#B5342E",
    "Fire Protection": "#B5342E",
    "Interior Design": "#7A4E8C",
    "Civil": "#5B6570",
    "Landscape": "#3F7A4E",
    "Unknown": "#8A8F94",
}


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


class SheetPickerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1220x760")
        self.minsize(980, 620)

        self.sheets: list[sc.SheetInfo] = []
        self.row_to_sheet: dict[str, sc.SheetInfo] = {}
        self._preview_token = 0
        self._preview_photo = None  # keep a reference so Tk doesn't GC it
        self._resize_after_id = None
        self._last_preview_sheet: sc.SheetInfo | None = None

        self._build_style()
        self._build_widgets()
        self._set_status("Choose a PDF file or a folder of PDFs to get started.")
        self._update_ocr_note()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        except tk.TclError:
            pass
        style.configure("Header.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("SubHeader.TLabel", font=("Segoe UI", 9), foreground="#5B6570")
        style.configure("Status.TLabel", font=("Segoe UI", 9), foreground="#3A3F44")
        style.configure("Big.TButton", font=("Segoe UI", 10, "bold"), padding=8)
        style.configure("Treeview", rowheight=24, font=("Segoe UI", 9.5))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def _build_widgets(self):
        root = ttk.Frame(self, padding=(12, 10, 12, 10))
        root.pack(fill="both", expand=True)
        root.rowconfigure(2, weight=1)
        root.columnconfigure(0, weight=1)

        # ---- toolbar -------------------------------------------------
        toolbar = ttk.Frame(root)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 4))

        ttk.Button(toolbar, text="Select PDF File...", style="Big.TButton",
                   command=self.on_select_pdf).pack(side="left")
        ttk.Button(toolbar, text="Select Folder of PDFs...", style="Big.TButton",
                   command=self.on_select_folder).pack(side="left", padx=(8, 0))

        self.ocr_note_var = tk.StringVar()
        ttk.Label(toolbar, textvariable=self.ocr_note_var, style="SubHeader.TLabel",
                  wraplength=380, justify="right").pack(side="right")

        ttk.Label(root,
                  text="Finds the best architectural floor plan sheet in a drawing set, "
                       "so you can export it for RF planning or 3D modeling import.",
                  style="SubHeader.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 8))

        # ---- main split: list (left) / preview (right) ---------------
        paned = ttk.Panedwindow(root, orient="horizontal")
        paned.grid(row=2, column=0, sticky="nsew")

        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=3)
        paned.add(right, weight=2)

        self._build_list_panel(left)
        self._build_preview_panel(right)

        # ---- export bar ------------------------------------------------
        self._build_export_bar(root)

        # ---- status bar -------------------------------------------------
        status_bar = ttk.Frame(root, relief="sunken", padding=(6, 3))
        status_bar.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.status_var = tk.StringVar()
        ttk.Label(status_bar, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(status_bar, mode="indeterminate", length=160)
        self.progress.pack(side="right")

    def _build_list_panel(self, parent):
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        ttk.Label(parent, text="Sheets found", style="Header.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4))

        cols = ("file", "page", "sheet", "discipline", "rec")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings",
                                  selectmode="extended", height=16)
        headings = {
            "file": ("File", 190),
            "page": ("Page", 50),
            "sheet": ("Sheet #", 80),
            "discipline": ("Discipline", 120),
            "rec": ("Recommended", 110),
        }
        for key, (label, width) in headings.items():
            self.tree.heading(key, text=label)
            anchor = "center" if key in ("page", "rec") else "w"
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "file"))

        vsb = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        vsb.grid(row=1, column=1, sticky="ns")

        self.tree.tag_configure("recommended", background="#FFF3D6")
        self.tree.tag_configure("unknown", foreground="#8A8F94")
        self.tree.bind("<<TreeviewSelect>>", self.on_row_selected)

        # "why this sheet" detail box
        detail = ttk.Frame(parent)
        detail.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        detail.columnconfigure(0, weight=1)
        ttk.Label(detail, text="Why this sheet", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        self.reason_var = tk.StringVar(value="Select a sheet from the list above.")
        ttk.Label(detail, textvariable=self.reason_var, wraplength=560, justify="left",
                  foreground="#2A2E32").grid(row=1, column=0, sticky="w", pady=(2, 0))

    def _build_preview_panel(self, parent):
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        ttk.Label(parent, text="Preview", style="Header.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4))

        self.preview_frame = ttk.Frame(parent, relief="groove", borderwidth=1)
        self.preview_frame.grid(row=1, column=0, sticky="nsew")
        self.preview_frame.rowconfigure(0, weight=1)
        self.preview_frame.columnconfigure(0, weight=1)

        self.preview_label = ttk.Label(self.preview_frame, anchor="center",
                                        text="No sheet selected yet",
                                        foreground="#8A8F94")
        self.preview_label.grid(row=0, column=0, sticky="nsew")
        self.preview_frame.bind("<Configure>", self._on_preview_resize)

        self.preview_caption_var = tk.StringVar()
        ttk.Label(parent, textvariable=self.preview_caption_var,
                  style="SubHeader.TLabel", wraplength=420).grid(
            row=2, column=0, sticky="w", pady=(6, 0))

    def _build_export_bar(self, parent):
        bar = ttk.Labelframe(parent, text="Export", padding=(10, 8))
        bar.grid(row=3, column=0, sticky="ew", pady=(10, 0))

        self.export_png_var = tk.BooleanVar(value=True)
        self.export_pdf_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Save as image (PNG)", variable=self.export_png_var).pack(side="left")
        ttk.Checkbutton(bar, text="Save as single-page PDF", variable=self.export_pdf_var).pack(
            side="left", padx=(14, 0))

        ttk.Label(bar, text="   Quality:").pack(side="left", padx=(14, 0))
        self.dpi_var = tk.IntVar(value=300)
        dpi_box = ttk.Combobox(bar, textvariable=self.dpi_var, width=6, state="readonly",
                                values=(150, 200, 300, 400, 600))
        dpi_box.pack(side="left")
        ttk.Label(bar, text="DPI").pack(side="left", padx=(4, 0))

        ttk.Button(bar, text="Export Selected Sheet(s)", style="Big.TButton",
                   command=self.on_export_selected).pack(side="right")
        ttk.Button(bar, text="Export All Recommended", style="Big.TButton",
                   command=self.on_export_recommended).pack(side="right", padx=(0, 8))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_status(self, text: str):
        self.status_var.set(text)

    def _update_ocr_note(self):
        if sc.ocr_available():
            self.ocr_note_var.set("")
        else:
            self.ocr_note_var.set(
                "Note: some PDFs store their text as pictures instead of real text.\n"
                "Reading those needs the optional text-recognition component, which "
                "isn't available right now — those sheets will show as “Unknown”.")

    def _busy(self, is_busy: bool):
        if is_busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    def run_in_background(self, fn, on_done=None, on_error=None):
        def worker():
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                if on_error:
                    self.after(0, lambda: on_error(exc))
                else:
                    self.after(0, lambda: messagebox.showerror(APP_TITLE, str(exc)))
                self.after(0, lambda: self._busy(False))
                return
            if on_done:
                self.after(0, lambda: on_done(result))
        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def on_select_pdf(self):
        path = filedialog.askopenfilename(
            title="Choose a PDF floor plan set",
            filetypes=[("PDF files", "*.pdf")])
        if path:
            self._start_scan(path)

    def on_select_folder(self):
        path = filedialog.askdirectory(title="Choose a folder of PDF floor plans")
        if path:
            self._start_scan(path)

    def _start_scan(self, path: str):
        self.tree.delete(*self.tree.get_children())
        self.row_to_sheet.clear()
        self.sheets = []
        self._show_preview_placeholder("Scanning...")
        self._set_status(f"Scanning {os.path.basename(path)}...")
        self._busy(True)

        def progress_cb(current, total, message):
            self.after(0, lambda: self._set_status(message))

        def work():
            return sc.scan_source(path, progress_cb=progress_cb)

        self.run_in_background(work, on_done=self._on_scan_done)

    def _on_scan_done(self, results: list[sc.SheetInfo]):
        self._busy(False)
        self.sheets = results

        if not results:
            self._set_status("No PDF files were found there.")
            messagebox.showinfo(APP_TITLE, "No PDF files were found in that location.")
            return

        for s in results:
            row_id = f"{s.file_path}::{s.page_index}"
            rec_text = "★ Recommended" if s.is_recommended else ""
            tags = []
            if s.is_recommended:
                tags.append("recommended")
            if s.discipline == "Unknown":
                tags.append("unknown")
            self.tree.insert("", "end", iid=row_id, tags=tuple(tags), values=(
                s.file_name, s.page_index + 1, s.sheet_number or "—",
                s.discipline, rec_text,
            ))
            self.row_to_sheet[row_id] = s

        recommended = [s for s in results if s.is_recommended]
        n_files = len({s.file_path for s in results})
        file_word = "file" if n_files == 1 else "files"
        if recommended:
            self._set_status(
                f"Done — {len(recommended)} recommended floor plan sheet"
                f"{'s' if len(recommended) != 1 else ''} found across {n_files} {file_word} "
                f"({len(results)} pages scanned).")
        else:
            self._set_status(
                f"Done — scanned {len(results)} page(s) across {n_files} {file_word}, "
                f"but nothing looked like a clear architectural floor plan. "
                f"Browse the list and pick one manually.")

        first_pick = recommended[0] if recommended else results[0]
        row_id = f"{first_pick.file_path}::{first_pick.page_index}"
        self.tree.selection_set(row_id)
        self.tree.see(row_id)
        self._update_ocr_note()

    # ------------------------------------------------------------------
    # Selection / preview
    # ------------------------------------------------------------------

    def _selected_sheets(self) -> list[sc.SheetInfo]:
        return [self.row_to_sheet[i] for i in self.tree.selection() if i in self.row_to_sheet]

    def on_row_selected(self, _event=None):
        sheets = self._selected_sheets()
        if not sheets:
            return
        s = sheets[-1]
        self._last_preview_sheet = s
        title = s.sheet_title if s.sheet_title != "(untitled)" else "(title not found)"
        header = f"{s.sheet_number or 'No sheet number found'} — {title}"
        self.reason_var.set(f"{header}\n{s.reason}")
        self.preview_caption_var.set(
            f"{s.file_name} — page {s.page_index + 1} of {s.page_count}  |  "
            f"{s.discipline}  |  {s.width_pt/72:.1f}\" × {s.height_pt/72:.1f}\" page")
        self._render_preview(s)

    def _show_preview_placeholder(self, text: str):
        self.preview_label.configure(image="", text=text)
        self._preview_photo = None

    def _render_preview(self, sheet: sc.SheetInfo):
        self._preview_token += 1
        token = self._preview_token
        self._show_preview_placeholder("Loading preview...")

        w = max(self.preview_frame.winfo_width() - 20, 300)
        h = max(self.preview_frame.winfo_height() - 20, 300)
        max_dim = max(w, h, 500)

        def work():
            return sc.render_thumbnail(sheet.file_path, sheet.page_index,
                                        max_dim=max_dim, rotation_fix=sheet.rotation_fix)

        def done(img: Image.Image):
            if token != self._preview_token:
                return  # a newer selection has since superseded this one
            fit = img.copy()
            fit.thumbnail((w, h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(fit)
            self._preview_photo = photo  # keep reference
            self.preview_label.configure(image=photo, text="")

        def error(exc):
            if token != self._preview_token:
                return
            self._show_preview_placeholder(f"Couldn't render this page:\n{exc}")

        self.run_in_background(work, on_done=done, on_error=error)

    def _on_preview_resize(self, _event=None):
        if self._resize_after_id:
            self.after_cancel(self._resize_after_id)
        self._resize_after_id = self.after(250, self._reflow_preview)

    def _reflow_preview(self):
        self._resize_after_id = None
        if self._last_preview_sheet is not None:
            self._render_preview(self._last_preview_sheet)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def on_export_selected(self):
        sheets = self._selected_sheets()
        if not sheets:
            messagebox.showinfo(APP_TITLE, "Select one or more sheets in the list first.")
            return
        self._export(sheets)

    def on_export_recommended(self):
        sheets = [s for s in self.sheets if s.is_recommended]
        if not sheets:
            messagebox.showinfo(APP_TITLE, "No recommended sheets to export yet — "
                                            "scan a PDF first, or export a manually chosen sheet instead.")
            return
        self._export(sheets)

    def _export(self, sheets: list[sc.SheetInfo]):
        if not self.export_png_var.get() and not self.export_pdf_var.get():
            messagebox.showinfo(APP_TITLE, "Choose at least one export format (PNG and/or PDF).")
            return

        out_dir = filedialog.askdirectory(title="Choose where to save the exported sheet(s)")
        if not out_dir:
            return

        self._busy(True)
        self._set_status(f"Exporting {len(sheets)} sheet(s)...")

        def progress_cb(current, total, message):
            self.after(0, lambda: self._set_status(f"{message}  ({current}/{total})"))

        def work():
            return sc.export_sheets(
                sheets, out_dir,
                dpi=self.dpi_var.get(),
                export_png=self.export_png_var.get(),
                export_pdf=self.export_pdf_var.get(),
                progress_cb=progress_cb,
            )

        def done(written_paths):
            self._busy(False)
            self._set_status(f"Exported successfully to {out_dir}")
            if messagebox.askyesno(
                    APP_TITLE,
                    f"Exported {len(written_paths)} file(s) to:\n{out_dir}\n\nOpen that folder now?"):
                self._open_folder(out_dir)

        def error(exc):
            self._busy(False)
            self._set_status("Export failed.")
            messagebox.showerror(APP_TITLE, f"Export failed:\n{exc}")

        self.run_in_background(work, on_done=done, on_error=error)

    @staticmethod
    def _open_folder(path: str):
        try:
            os.startfile(path)  # noqa: S606  (Windows-only helper app)
        except Exception:
            webbrowser.open(path)


def main():
    app = SheetPickerApp()
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        app.after(200, lambda: app._start_scan(sys.argv[1]))
    app.mainloop()


if __name__ == "__main__":
    main()
