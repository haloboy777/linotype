<p align="center">
  <img src="engine/assets/icon-source.png" width="112" alt="Linotype logo: a serif L on a black rounded square">
</p>
<h1 align="center">Linotype</h1>
<p align="center">Edit the text of any PDF in place, in its own fonts.</p>
<p align="center"><a href="https://pdf.ayushsingh.dev">pdf.ayushsingh.dev</a> · AGPL-3.0</p>

Drop a PDF in, click any text, type, save. Linotype shows each page exactly as it looks, lets the text you click reflow as you type in the same typeface, and writes the change back into the original file. Everything you didn't touch stays byte-for-byte the same, and the new text is real, searchable text. The desktop app never sends anything off your computer; the website keeps your file in memory only while you edit it.

## Use it

**Desktop app** (macOS 11+ on Apple Silicon for now): open Linotype, drop a PDF on the window, or right-click a PDF → Open With → Linotype. Edit, then **Save PDF** (⌘S). The edited copy is written as `<name>-edited.pdf` and opens in your PDF viewer.

**From source:**

```
cd engine
uv run desktop.py          # native window (pywebview)
uv run desktop.py file.pdf # straight into the editor; saves next to the file
uv run app.py              # same thing in your browser instead of a window
```

## Layout

Everything is Python, in `engine/`:

```
render.py     PDF page → editable HTML in the PDF's own fonts (PyMuPDF, fontTools)
editor.py     click-to-edit boxes that reflow in the same fonts
writeback.py  edits → the original PDF
app.py        local server (127.0.0.1): drop page, editor, save
desktop.py    native window around app.py (pywebview: WKWebView on macOS, WebView2 on Windows)
harness.py    scores renders against the real page
```

Build the app:

```
cd engine
./build_app.sh             # → dist/Linotype.app and dist/Linotype.dmg (PyInstaller, linotype.spec)
```

The build first syntax-checks the pages' JavaScript, which lives in Python strings. Finder "Open With" and drops on the Dock icon arrive as `application:openURLs:` on the app delegate; `desktop.py` extends pywebview's delegate to handle them.

## Server mode

Linotype also runs as a website: `LINOTYPE_MODE=server` adds a password login, keeps each visitor's PDFs and added fonts in memory only (dropped after 30 minutes idle, never written to disk), and makes Save a download. Settings are environment variables, listed at the top of `engine/app.py`. The drop page links to `/source.tar.gz`, since the AGPL requires offering the source to people who use it over a network.

Running at pdf.ayushsingh.dev on the Hetzner box:

- code in `~/apps/linotype`, uv venv on Python 3.12, `fonts-noto-core` for Hindi
- `/etc/linotype/linotype.env` (root, 600): mode, password, `127.0.0.1:8091`, limits
- `linotype.service`: `Restart=always`, enabled at boot, `MemoryMax=1200M`, read-only filesystem
- nginx site `pdf.ayushsingh.dev` → `127.0.0.1:8091`, Cloudflare origin cert, 60 MB uploads

Update it with `engine/deploy_server.sh`. Change the password by editing the env file, then `sudo systemctl restart linotype` (this also signs everyone out).

## How it works

**Showing a page** (`engine/render.py`). Every glyph comes out of MuPDF with its font, size, color and position. Each run becomes an SVG `<text>` with one x per glyph, so spacing matches exactly. Everything that isn't text (photos, rules, gradients) is one image rendered from a copy of the page with its text made invisible. The fonts are the PDF's own, converted for the browser:

- CFF subsets (InDesign, Distiller, Word) wrapped into OTF
- Type 1 (LaTeX) outlines redrawn into CFF
- TrueType, including Chrome/Skia `Type0/Identity-H` fonts, mapped through `/ToUnicode`
- one font name split across many subsets (Chrome does this) becomes a chain of `@font-face`s; each character comes from the first subset that has it
- character maps from glyph names plus the PDF's `/Encoding /Differences` and `/ToUnicode`
- broken text layers repaired: ToUnicode disagreeing with glyph names, and fonts in Mac glyph order where every letter is shifted by 29 (`*HQWO\` → `Gently`)
- anything missing falls back to a system font of the same style (serif, sans, mono)
- shaped scripts (Hindi and other Indic scripts, Arabic, Thai): drawn whole words at a time, stretched to the PDF's word width, so the browser can join letters, attach vowel signs and form conjuncts. Subsets that kept only finished glyphs fall back to a system font for that script (Kohinoor Devanagari, Nirmala UI, Noto)

Only fonts reachable through the page's actual `Do` calls count, because resource dicts are often shared by every page.

**Adding the original fonts.** PDFs usually carry only trimmed copies of their fonts, sometimes too trimmed to edit with (Google Docs' Hindi subsets keep only finished letter shapes). **Aa Fonts** lists every font the text uses, how much of the text it covers and whether the PDF's copy is usable. Add a `.ttf`, `.otf`, `.ttc`, `.woff` or `.woff2` file per font, via the button or by dropping it on the row. It then comes first for viewing, editing and saving, and is subset on save. Fonts whose license says "restricted" (`fsType` 2) or "bitmaps only" are refused; "view & print only" and name mismatches get a warning. Added fonts live in memory for as long as the app runs and are never written to disk.

**Editing** (`engine/editor.py`). Each text block gets an invisible `contenteditable` box over its glyphs. Clicking swaps the exact glyphs for the box, which reflows in the same fonts:

- blocks are split into columns; word fragments on one baseline are rejoined
- a line ends *by choice* when the next line's first word would have fit on it: that's a paragraph end, list item or table cell. Other line ends are wraps, and only those reflow
- indents and hanging bullets become `text-indent`; centered text keeps its breaks
- letter-spacing and word-spacing come from the glyph positions; spaces the PDF left out are put back
- on load each box nudges its letter-spacing until it wraps into the PDF's line count
- a box that grows past its space is outlined red with `+N lines`
- rotated or translucent text and legal notices are locked

**Keep original** (on by default, next to Save) attaches `linotype-original.pdf` and `linotype-state.json` (what each edited box holds) to the saved file. Opening that file in Linotype edits the original again with the saved edits restored as live boxes, and offers **Revert to original**. Anyone with the file can extract the original, so it's a toggle. Files saved without it are still read back: Linotype tags its shaped-script words and reads its own tags on reopen.

**Saving** (`engine/writeback.py`). For each edited block, the old text is removed with a text-only redaction (images and vector art stay), and every glyph is placed where the browser drew it, in the same embedded font. Shaped scripts go word by word through MuPDF's HTML engine (HarfBuzz) in the same font the browser used, all words of a block on one layer so the font is embedded once, wrapped in `/ActualText` so copy and search return the real text. Baselines come from the browser: a character's box top plus its font's ascent.

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

- Scanned pages are pictures of text, and Canva exports draw every letter as a shape: there is no text to edit without OCR.
- Password-protected PDFs open only with their password. **Remove password** (on by default for those files) saves a copy without it; untick it to keep the same password. There is no way in without the password.
- CID-keyed CFF fonts fall back to system fonts.
- Glyphs with no Unicode mapping (some chart symbols) draw nothing.
- Text painted underneath an image ends up on top of it.
- Rotated pages aren't handled on save yet.
- Hindi and other shaped scripts use a system font when the PDF's subset can't shape, so an edited paragraph can wrap differently from the original.

## License

AGPL-3.0-or-later (Linotype builds on PyMuPDF, which is AGPL).
