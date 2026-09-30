"""Write edits from the browser back into the original PDF.

Each edited block arrives as its original bbox plus every glyph the browser drew: character,
x, baseline y, font family, size, color. The old text inside the bbox is removed with a
text-only redaction (images and vector art stay), then each glyph is placed where the browser
put it, in the font the PDF already embedded. Everything that wasn't edited is left alone.
"""
import pymupdf

# Base-14 stand-ins matching the browser's fallback chain: (regular, bold)
BASE14 = {"serif": ("tiro", "tibo"), "monospace": ("cour", "cobo"), "sans-serif": ("helv", "hebo")}


def apply(pdf_bytes, edits, fonts):
    """edits: [{page, blocks: [{bbox, glyphs: [{c, x, y, fams, size, color, bold}]}]}]
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

    return doc.tobytes(garbage=3, deflate=True)
