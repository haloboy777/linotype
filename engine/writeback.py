"""Write edits from the browser back into the original PDF.

Each edited block arrives as its original bbox plus every glyph the browser drew: character,
x, baseline y, font family, size, color. The old text inside the bbox is removed with a
text-only redaction (images and vector art stay), then each glyph is placed where the browser
put it, in the font the PDF already embedded. Everything that wasn't edited is left alone.
"""
import io
import os
import re
import tempfile
from pathlib import Path

import pymupdf

# Base-14 stand-ins matching the browser's fallback chain: (regular, bold)
BASE14 = {"serif": ("tiro", "tibo"), "monospace": ("cour", "cobo"), "sans-serif": ("helv", "hebo")}


# System fonts that shape complex scripts, in the order the editor's CSS falls back to them
SHAPED_FONTS = [
    "/System/Library/Fonts/Kohinoor.ttc",                      # macOS: Devanagari
    "/System/Library/Fonts/Supplemental/GeezaPro.ttc",         # macOS: Arabic
    "/System/Library/Fonts/Supplemental/Thonburi.ttc",         # macOS: Thai
    "C:/Windows/Fonts/Nirmala.ttf",                            # Windows: Indic scripts
    "C:/Windows/Fonts/segoeui.ttf",                            # Windows: Arabic, Hebrew
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
]
_covers, _baseline = {}, {}


def _font_file_for(text, embedded, tmp):
    """The font MuPDF should shape this word with: the PDF's own font if the editor used it,
    else the first system font that has every letter (what the browser fell back to)."""
    if embedded:
        path = Path(tmp) / f"{embedded[0]}.ttf"
        if not path.exists():
            path.write_bytes(embedded[1])
        return str(path)
    for f in SHAPED_FONTS:
        if f not in _covers:
            _covers[f] = pymupdf.Font(fontfile=f) if os.path.exists(f) else None
        font = _covers[f]
        if font and all(font.has_glyph(ord(ch)) for ch in text if not ch.isspace()):
            return f
    return None  # MuPDF's built-in Noto fonts


def _css(fontfile):
    face = f"@font-face{{font-family:W;src:url({Path(fontfile).name});}}" if fontfile else ""
    # MuPDF's own stylesheet gives <body> a 1em margin; without the !important overrides every
    # word would land one em right of (and below) where it was placed
    css = face + (f"html,body,*{{font-family:{'W' if fontfile else 'sans-serif'};white-space:nowrap;"
                  "margin:0!important;padding:0!important;line-height:1.2}}")
    return css, pymupdf.Archive(str(Path(fontfile).parent)) if fontfile else None


def _span(w):
    r, g, b = (int(v * 255) for v in w["color"])
    text = w["text"].replace("&", "&amp;").replace("<", "&lt;")
    return f"<span style='font-size:{w['size']}pt;color:rgb({r},{g},{b})'>{text}</span>"


def _origin_offset(w, fontfile):
    """Where a story's first glyph lands relative to the box it was placed in (dx, dy), for this
    font and size: the baseline sits below the top, and any leftover margin shifts it right."""
    key = (fontfile, round(w["size"], 2))
    if key not in _baseline:
        css, archive = _css(fontfile)
        probe = pymupdf.open().new_page()
        probe.insert_htmlbox(pymupdf.Rect(0, 0, 500, 200), _span(w), css=css, archive=archive)
        chars = [c for b in probe.get_text("rawdict")["blocks"] for l in b.get("lines", [])
                 for s in l["spans"] for c in s["chars"]]
        _baseline[key] = (chars[0]["origin"][0], chars[0]["origin"][1]) if chars else (0.0, w["size"] * 0.95)
    return _baseline[key]


def _top_level_groups(content):
    """Split a content stream into its top-level q ... Q groups (one per story drawn)."""
    groups, depth, start = [], 0, None
    for m in re.finditer(rb"(?<![^\s\]>)])(q|Q)(?=\s|$)", content):
        if m.group(1) == b"q":
            if depth == 0:
                start = m.start()
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                groups.append((start, m.end()))
    return groups


def _actual_text(text):
    return b"/Span <</ActualText <FEFF" + text.encode("utf-16-be").hex().upper().encode() + b">>> BDC\n"


def _shaped_block(page, block, fonts, tmp):
    """Shaped scripts (Hindi, Arabic, Thai...): MuPDF's HTML engine shapes each word with
    HarfBuzz at the position the browser gave it. All of a block's words go onto one scratch
    page, so the font is embedded once, and that page is stamped onto the real one.

    MuPDF writes unreliable text data for shaped glyphs (conjuncts come out as garbage), so each
    word is wrapped in its own /ActualText span. Per word, not per block: a block-wide span
    makes readers put the whole paragraph's text at one spot."""
    out = io.BytesIO()
    writer = pymupdf.DocumentWriter(out)
    dev = writer.begin_page(page.rect)
    drawn = []
    for w in block["words"]:
        embedded = (w["fam"], fonts[w["fam"]][1]) if w["fam"] in fonts else None
        fontfile = _font_file_for(w["text"], embedded, tmp)
        css, archive = _css(fontfile)
        dx, dy = _origin_offset(w, fontfile)
        left, top = w["x"] - dx, w["y"] - dy
        story = pymupdf.Story(html=_span(w), user_css=css, archive=archive)
        story.place(pymupdf.Rect(left, top, page.rect.width + 500, top + w["size"] * 3))
        story.draw(dev)
        drawn.append(w["text"])
    writer.end_page()
    writer.close()

    scratch = pymupdf.open("pdf", out.getvalue())
    xref = scratch[0].get_contents()[0]
    content = scratch.xref_stream(xref)
    groups = _top_level_groups(content)
    if len(groups) == len(drawn):  # one group per word, in order: tag each with its text
        parts, last = [], 0
        for (a, b), text in zip(groups, drawn):
            parts += [content[last:a], _actual_text(text), content[a:b], b"\nEMC\n"]
            last = b
        parts.append(content[last:])
        scratch.update_stream(xref, b"".join(parts))
    before = {x for x in range(1, page.parent.xref_length())}
    page.show_pdf_page(page.rect, scratch, 0)
    # mark the stamped layer as Linotype's, so opening this file again can read its words
    # from the tags instead of from MuPDF's reading of shaped glyphs
    for x in range(1, page.parent.xref_length()):
        if x not in before and page.parent.xref_get_key(x, "Subtype")[1] == "/Form":
            page.parent.xref_set_key(x, "LinotypeShaped", "true")


def apply(pdf_bytes, edits, fonts):
    """edits: [{page, blocks: [{bbox, glyphs: [{c, x, y, fams, size, color, bold}], words, text}]}]
    bbox is null for a new text box; a deleted block has a bbox and no glyphs or words.
    fonts: {css family: (kind, font bytes, codepoints)} from render_page(font_sink=...)"""
    doc = pymupdf.open("pdf", pdf_bytes)
    loaded = {}

    def font_for(g):
        # first family in the chain whose font has this char, like the browser did
        for fam in g["fams"]:
            f = fonts.get(fam)
            if f and ord(g["c"]) in f[2]:
                if fam not in loaded:
                    loaded[fam] = pymupdf.Font(fontbuffer=f[1])
                return loaded[fam]
        generic = next((k for k in BASE14 if k in g["fams"]), "sans-serif")
        key = BASE14[generic][bool(g.get("bold"))]
        if key not in loaded:
            loaded[key] = pymupdf.Font(key)
        return loaded[key]

    for pe in edits:
        page = doc[pe["page"]]
        for b in pe["blocks"]:
            if b.get("bbox"):  # new text boxes have no old text to remove
                page.add_redact_annot(pymupdf.Rect(b["bbox"]))
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
        writers = {}  # one TextWriter per color
        for b in pe["blocks"]:
            for g in b["glyphs"]:
                color = tuple(round(v, 3) for v in g["color"])
                tw = writers.setdefault(color, pymupdf.TextWriter(page.rect))
                tw.append((g["x"], g["y"]), g["c"], font=font_for(g), fontsize=g["size"])
        for color, tw in writers.items():
            tw.write_text(page, color=color)
        # shaped scripts (Hindi, Arabic, Thai...): whole words, shaped by MuPDF
        with tempfile.TemporaryDirectory() as tmp:
            for b in pe["blocks"]:
                if b.get("words"):
                    _shaped_block(page, b, fonts, tmp)

    used_user_font = any(f.startswith("user") for pe in edits for b in pe["blocks"]
                         for g in b.get("glyphs", []) + b.get("words", []) for f in g.get("fams", [g.get("fam", "")]))
    shaped_words = any(b.get("words") for pe in edits for b in pe["blocks"])
    if used_user_font or shaped_words:  # the shaped path embeds whole system fonts
        try:
            # a full font the user added would otherwise go in whole; keep only the letters used
            doc.subset_fonts()
        except Exception:
            pass
    return doc.tobytes(garbage=3, deflate=True)
