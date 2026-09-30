# Linotype

Edit the text of any PDF in place, in its own fonts.

Drop a PDF in, click any text, type, save. Linotype shows each page exactly as it looks, lets the text you click reflow as you type in the same typeface, and writes the change back into the original file. Everything you didn't touch stays byte-for-byte the same, and the new text is real, searchable text. Nothing leaves your computer.

## Use it

**Desktop app** (macOS for now): open Linotype, drop a PDF on the window, or right-click a PDF → Open With → Linotype. Edit, then **Save PDF** (⌘S). The edited copy is written as `<name>-edited.pdf` and opens in your PDF viewer.

**From source, in your browser:**

```
cd engine
uv run app.py              # drop page
uv run app.py file.pdf     # straight into the editor; saves next to the file
```

## Layout

```
engine/    Python: PDF → editable HTML, and edits → PDF (PyMuPDF, fontTools)
desktop/   Tauri shell: native window; runs the engine as a sidecar
```

Build the app:

```
cd desktop
pnpm install
pnpm sidecar               # freezes the engine into src-tauri/binaries/ (PyInstaller)
pnpm tauri build           # → src-tauri/target/release/bundle/macos/Linotype.app
```

The shell starts `linotype-engine`, reads the `LINOTYPE_URL` / `LINOTYPE_TOKEN` lines it prints, and points the window at it. Files opened from Finder go through `/open-path`, which needs the token.

## How it works

**Showing a page** (`engine/render.py`). Every glyph comes out of MuPDF with its font, size, color and position. Each run becomes an SVG `<text>` with one x per glyph, so spacing matches exactly. Everything that isn't text (photos, rules, gradients) is one image rendered from a copy of the page with its text made invisible. The fonts are the PDF's own, converted for the browser:

- CFF subsets (InDesign, Distiller, Word) wrapped into OTF
- Type 1 (LaTeX) outlines redrawn into CFF
- TrueType, including Chrome/Skia `Type0/Identity-H` fonts, mapped through `/ToUnicode`
- one font name split across many subsets (Chrome does this) becomes a chain of `@font-face`s; each character comes from the first subset that has it
- character maps from glyph names plus the PDF's `/Encoding /Differences` and `/ToUnicode`
- broken text layers repaired: ToUnicode disagreeing with glyph names, and fonts in Mac glyph order where every letter is shifted by 29 (`*HQWO\` → `Gently`)
- anything missing falls back to a system font of the same style (serif, sans, mono)

Only fonts reachable through the page's actual `Do` calls count, because resource dicts are often shared by every page.

**Editing** (`engine/editor.py`). Each text block gets an invisible `contenteditable` box over its glyphs. Clicking swaps the exact glyphs for the box, which reflows in the same fonts:

- blocks are split into columns; word fragments on one baseline are rejoined
- a line ends *by choice* when the next line's first word would have fit on it: that's a paragraph end, list item or table cell. Other line ends are wraps, and only those reflow
- indents and hanging bullets become `text-indent`; centered text keeps its breaks
- letter-spacing and word-spacing come from the glyph positions; spaces the PDF left out are put back
- on load each box nudges its letter-spacing until it wraps into the PDF's line count
- a box that grows past its space is outlined red with `+N lines`
- rotated or translucent text and legal notices are locked

**Saving** (`engine/writeback.py`). For each edited block, the old text is removed with a text-only redaction (images and vector art stay), and every glyph is placed where the browser drew it, in the same embedded font.

## Test harness

```
cd engine
uv run harness.py file.pdf --pages 1-3 --editable --open
```

For each page this writes the render, a raster of the real page, a Chrome screenshot and a diff, and reports:

- `mismatch`: % of pixels that differ from the real page (after a 1px blur, so anti-aliasing doesn't count)
- `same line count`: % of blocks that, switched to editable reflow, wrap into as many lines as the PDF
- which fonts were embedded, fell back or were repaired

Output lands in `engine/out/`, which is git-ignored because it holds renders of your documents.

| Tested with | Made by | Mismatch | Same line count |
|---|---|---|---|
| Wall Street Journal | InDesign | 0.02–0.03% | 99.1–99.6% |
| ACM paper | LaTeX | 0.03–0.05% | 100% |
| SSRN paper | Word | 0.00% | 100% |
| Xbox manual | InDesign → Distiller | 0.00% | 100% |
| Annual report | Google Slides | 0.00% | 100% |
| Receipt, e-ticket | Chrome | 0.00–0.01% | 100% |

`NO_BROWSER=1 NO_OPEN=1 uv run app.py file.pdf`, then open `<url>#selftest` in headless Chrome: it edits two blocks and saves, end to end.

## Known gaps

- Canva exports draw every letter as a shape; there is no text to edit without OCR.
- CID-keyed CFF fonts fall back to system fonts.
- Glyphs with no Unicode mapping (some chart symbols) draw nothing.
- Text painted underneath an image ends up on top of it.
- Rotated pages aren't handled on save yet.

## License

AGPL-3.0-or-later (Linotype builds on PyMuPDF, which is AGPL).
