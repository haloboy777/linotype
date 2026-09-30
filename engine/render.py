"""Render one PDF page as positioned HTML (inline SVG) using the PDF's own embedded fonts.

    uv run render.py file.pdf --page 2 --out page2.html
"""
import argparse
import base64
import html
import io
import logging
import math
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field

import pymupdf
from fontTools import agl
from fontTools.cffLib import CFFFontSet
from fontTools.encodings.StandardEncoding import StandardEncoding
from fontTools.fontBuilder import FontBuilder
from fontTools.misc.psCharStrings import T2CharString
from fontTools.pens.basePen import NullPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.t1Lib import T1Font
from fontTools.ttLib import TTFont, newTable

import editor

logging.getLogger("fontTools").setLevel(logging.ERROR)

REF = re.compile(r"/([^\s/<>\[\]()]+)\s+(\d+)\s+0\s+R")
DO = re.compile(r"/([^\s/<>\[\]()]+)\s+Do\b")
FALLBACK = "Helvetica,Arial,sans-serif"
# Every glyph is placed from the PDF, so the browser must not merge or re-space any of them.
NO_LIGATURES = ("text,.ed{font-variant-ligatures:none;"
                "font-feature-settings:'liga' 0,'clig' 0,'dlig' 0,'hlig' 0;font-kerning:none}")


def fallback_for(font_name):
    """System fonts to use when a glyph (or the whole font) isn't available: keep the style."""
    n = font_name.lower()
    if any(k in n for k in ("courier", "mono", "consol", "menlo")):
        return "'Courier New',Courier,monospace"
    if any(k in n for k in ("times", "serif", "roman", "georgia", "garamond", "minion", "libertine")) \
            and "sans" not in n:
        return "'Times New Roman',Times,serif"
    return FALLBACK


@dataclass
class Stats:
    page: int
    fonts_used: int = 0
    fonts_embedded: int = 0
    fallback_fonts: list = field(default_factory=list)
    repaired_fonts: list = field(default_factory=list)
    glyphs_missing: int = 0
    chars: int = 0
    images: int = 0
    drawings: int = 0
    rotated_lines: int = 0
    shades_skipped: int = 0
    bg: str = ""
    edit_blocks: int = 0
    locked_blocks: int = 0


# ---------- fonts ----------

def _name_unicode(name):
    """Glyph name -> text. Adds the short `u572` form MuPDF accepts but the AGL spec doesn't."""
    u = agl.toUnicode(name)
    if not u and (m := re.fullmatch(r"u([0-9A-Fa-f]{3,6})", name)):
        u = chr(int(m.group(1), 16))
    return u


def _tounicode(doc, xref):
    """code -> text from the font's /ToUnicode CMap."""
    kind, val = doc.xref_get_key(xref, "ToUnicode")
    if kind != "xref":
        return {}
    data = doc.xref_stream(int(val.split()[0])).decode("latin1")
    u16 = lambda h: bytes.fromhex(h).decode("utf-16-be", "ignore")
    m = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            m[int(src, 16)] = u16(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", data, re.S):
        for a, b, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", block):
            a, b = int(a, 16), int(b, 16)
            if dst.startswith("["):
                for i, h in enumerate(re.findall(r"<([0-9A-Fa-f]+)>", dst)):
                    m[a + i] = u16(h)
            elif len(first := u16(dst[1:-1])) == 1:
                for i in range(b - a + 1):
                    m[a + i] = chr(ord(first) + i)
    return m


def _pdf_cmap(doc, xref, builtin):
    """unicode -> glyph name, from what the PDF itself says: the font's built-in encoding,
    overridden by /Encoding /Differences, joined with /ToUnicode. Catches glyphs whose
    names mean nothing (g12, uni-less custom names) that name-based mapping misses."""
    enc = list(builtin) + [None] * (256 - len(builtin))
    kind, val = doc.xref_get_key(xref, "Encoding")
    if kind != "name":
        kind, val = doc.xref_get_key(xref, "Encoding/BaseEncoding")
    if kind == "name" and val in ("/WinAnsiEncoding", "/MacRomanEncoding"):
        codec = "cp1252" if "WinAnsi" in val else "mac_roman"
        enc = [agl.UV2AGL.get(ord(bytes([c]).decode(codec, "ignore") or "\0")) for c in range(256)]
    kind, diffs = doc.xref_get_key(xref, "Encoding/Differences")
    if kind == "array":
        code = 0
        for tok in re.findall(r"\d+|/[^\s/\[\]]+", diffs):
            if tok.isdigit():
                code = int(tok)
            elif code < 256:
                enc[code] = tok[1:]
                code += 1
    tu = _tounicode(doc, xref)
    # Some producers write a /ToUnicode that is off by a constant (here: every letter shifted
    # by 29), so extracted text is garbage while the glyphs draw fine. When most codes'
    # ToUnicode disagrees with their glyph names, trust the names and repair the text.
    named = {c: _name_unicode(n) for c, n in enumerate(enc[:256]) if n and len(_name_unicode(n)) == 1}
    both = [c for c in named if len(tu.get(c, "")) == 1]
    broken = len(both) >= 5 and sum(tu[c] != named[c] for c in both) > 0.5 * len(both)
    repair = {tu[c]: named[c] for c in both if tu[c] != named[c]} if broken else {}
    out = {}
    for code, name in enumerate(enc[:256]):
        u = named.get(code, "") if broken else (tu.get(code) or named.get(code, ""))
        if name and len(u) == 1:
            out[ord(u)] = name
    return out, repair


def _finish_otf(fb, upm, order, metrics, cmap):
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(cmap)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=int(upm * 0.9), descent=-int(upm * 0.25))
    fb.setupNameTable({"familyName": "F", "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=int(upm * 0.9), sTypoDescender=-int(upm * 0.25),
                usWinAscent=int(upm * 0.9), usWinDescent=int(upm * 0.25))
    fb.setupPost()


def _name_cmap(order, pdf_map):
    pdf_map, repair = pdf_map if isinstance(pdf_map, tuple) else (pdf_map, {})
    cmap = {}
    for g in order:
        u = _name_unicode(g)
        if len(u) == 1:
            cmap.setdefault(ord(u), g)
    cmap.update({u: g for u, g in pdf_map.items() if g in order})
    return cmap, repair


def cff_to_otf(buf, space_width=None, pdf_map=None, doc=None, xref=None):
    """Wrap a bare CFF font program (as embedded in a PDF) into an OTF browsers will load."""
    cff = CFFFontSet()
    cff.decompile(io.BytesIO(buf), None)
    top = cff[cff.fontNames[0]]
    if hasattr(top, "ROS"):
        raise ValueError("CID-keyed CFF not supported")
    order = list(top.charset)
    metrics = {}
    for g in order:
        cs = top.CharStrings[g]
        cs.draw(NullPen())
        metrics[g] = (int(round(cs.width)), 0)
    builtin = top.Encoding if isinstance(top.Encoding, list) else StandardEncoding
    cmap, repair = _name_cmap(order, _pdf_cmap(doc, xref, builtin) if doc else {})
    upm = int(round(1 / top.FontMatrix[0])) if top.FontMatrix[0] else 1000
    if 32 not in cmap:
        # Subsets often drop the space glyph (the PDF positions words instead). Without one the
        # browser borrows a space from some other font, and reflowed lines come out wider.
        priv = top.Private
        w = int(space_width * upm / 1000) if space_width else upm // 4
        prog = ([w - priv.nominalWidthX] if w != priv.defaultWidthX else []) + ["endchar"]
        cs = T2CharString(program=prog, private=priv, globalSubrs=cff.GlobalSubrs)
        top.CharStrings.charStringsIndex.append(cs)
        top.CharStrings.charStrings["space"] = len(top.CharStrings.charStringsIndex) - 1
        top.charset.append("space")
        order.append("space")
        metrics["space"] = (w, 0)
        cmap[32] = "space"
    fb = FontBuilder(upm, isTTF=False)
    _finish_otf(fb, upm, order, metrics, cmap)
    fb.font["CFF "] = newTable("CFF ")
    fb.font["CFF "].cff = cff
    fb.font.sfntVersion = "OTTO"  # setupCFF would set this; browsers forgive it, FreeType doesn't
    fb.setupMaxp()
    fb.setupHead()
    out = io.BytesIO()
    fb.save(out)
    return "otf", out.getvalue(), set(cmap), repair


def type1_to_otf(buf, space_width=None, doc=None, xref=None):
    """Type 1 (what LaTeX embeds) -> OTF: redraw each outline into a CFF charstring."""
    if b"cleartomark" not in buf[-600:]:
        # PDFs embed Type 1 without the trailing zeros + cleartomark; fontTools needs them
        buf = buf + b"\n" + b"0" * 512 + b"\ncleartomark\n"
    with tempfile.NamedTemporaryFile(suffix=".pfa") as tmp:
        tmp.write(buf)
        tmp.flush()
        t1 = T1Font(tmp.name)
        t1.parse()
    glyphs = t1.getGlyphSet()
    order = [".notdef"] + sorted(g for g in glyphs.keys() if g != ".notdef")
    charstrings, metrics = {}, {}
    for g in order:
        if g not in glyphs:  # a subset without .notdef
            charstrings[g] = T2CharStringPen(0, None).getCharString()
            metrics[g] = (0, 0)
            continue
        glyphs[g].draw(NullPen())
        w = int(round(glyphs[g].width))
        pen = T2CharStringPen(w, glyphs)
        glyphs[g].draw(pen)
        charstrings[g], metrics[g] = pen.getCharString(), (w, 0)
    enc = t1.font.get("Encoding")
    builtin = enc if isinstance(enc, list) else StandardEncoding
    cmap, repair = _name_cmap(order, _pdf_cmap(doc, xref, builtin) if doc else {})
    if 32 not in cmap:
        w = int(space_width) if space_width else 250
        order.append("space")
        charstrings["space"], metrics["space"] = T2CharStringPen(w, None).getCharString(), (w, 0)
        cmap[32] = "space"
    fb = FontBuilder(1000, isTTF=False)
    _finish_otf(fb, 1000, order, metrics, cmap)
    name = t1.font.get("FontName", "F")
    fb.setupCFF(name, {"FullName": name}, charstrings, {})
    fb.setupMaxp()
    fb.setupHead()
    out = io.BytesIO()
    fb.save(out)
    return "otf", out.getvalue(), set(cmap), repair


def _complete_sfnt(font, cmap):
    """Give a stripped embedded subset the tables browsers insist on, with our cmap."""
    fb = FontBuilder(font=font)
    fb.setupCharacterMap(cmap)
    upm = font["head"].unitsPerEm
    if "name" not in font:
        fb.setupNameTable({"familyName": "F", "styleName": "Regular"})
    if "OS/2" not in font:
        fb.setupOS2(sTypoAscender=int(upm * 0.9), sTypoDescender=-int(upm * 0.25),
                    usWinAscent=int(upm * 0.9), usWinDescent=int(upm * 0.25))
    if "post" not in font:
        fb.setupPost()
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def ttf_to_web(buf, doc=None, xref=None):
    """Embedded TrueType. Simple fonts keep their cmap; Type0/Identity-H fonts (what Chrome,
    Skia and most HTML-to-PDF tools write) address glyphs by id, so the only reliable
    text mapping is /ToUnicode: code = CID = glyph id."""
    tt = TTFont(io.BytesIO(buf))
    order = tt.getGlyphOrder()
    cmap = dict(tt.getBestCmap() or {}) if "cmap" in tt else {}
    if doc is not None and doc.xref_get_key(xref, "Subtype")[1] == "/Type0":
        from_tu = {}
        for cid, text in _tounicode(doc, xref).items():
            if len(text) == 1 and ord(text) >= 32 and cid < len(order):
                from_tu.setdefault(ord(text), order[cid])
        # Chrome writes \x00 for glyphs it can't name (hyphen.case, plus.case, ...);
        # the glyph names in the font's post table still say what they are
        for name in order:
            u = _name_unicode(name.split(".")[0])
            if len(u) == 1 and ord(u) >= 32:
                from_tu.setdefault(ord(u), name)
        cmap = from_tu or cmap
    if not cmap:
        raise ValueError("TrueType without any text mapping")
    return "ttf", _complete_sfnt(tt, cmap), set(cmap), {}


def to_webfont(doc, xref, space_width=None, shift=0):
    _, ext, _, buf = doc.extract_font(xref)
    if not buf:
        raise ValueError("not embedded")
    if ext == "cff":
        kind, data, cov, repair = cff_to_otf(buf, space_width, doc=doc, xref=xref)
    elif ext in ("pfa", "pfb"):
        kind, data, cov, repair = type1_to_otf(buf, space_width, doc=doc, xref=xref)
    elif ext == "ttf":
        kind, data, cov, repair = ttf_to_web(buf, doc, xref)
    else:
        raise ValueError(f"unsupported embedded type: {ext}")
    if shift:
        # text and glyph names are both offset; move the font's cmap to the real characters
        font = TTFont(io.BytesIO(data))
        for table in font["cmap"].tables:
            table.cmap = {(u + shift if u < 0x80 else u): g for u, g in table.cmap.items()}
        out = io.BytesIO()
        font.save(out)
        data = out.getvalue()
        cov = set(font.getBestCmap())
        repair = {chr(u): chr(u + shift) for u in range(0x80)}
    return kind, data, cov, repair


def glyph_order_shift(text):
    """Fonts subset in the standard Mac glyph order, with a ToUnicode that just echoes glyph ids,
    come out as ASCII shifted down by 29 (space is \\x03, "Gently" reads "*HQWO\\"). Spot it by a
    control char doing the job of the space, and return the shift that puts it back at 32."""
    ctrl = [ch for ch in text if ord(ch) < 32 and ch not in "\t\n\r"]
    if not ctrl or len(ctrl) < 0.08 * len(text):
        return 0
    top = max(set(ctrl), key=ctrl.count)
    return 32 - ord(top) if ctrl.count(top) >= 0.5 * len(ctrl) else 0


# ---------- which font objects does this page actually draw with? ----------

def _dict_refs(doc, xref, key):
    kind, val = doc.xref_get_key(xref, key)
    if kind == "xref":  # the dict itself is an indirect object
        val = doc.xref_object(int(val.split()[0]))
    return dict((name, int(num)) for name, num in REF.findall(val or ""))


def page_resources(doc, page):
    """(font xrefs, form xobject xrefs) reachable from what the page draws. Resource dicts are
    often shared by every page, so follow the actual `Do` calls instead of the resource list."""
    fonts, seen = set(), set()

    def walk(owner, content):
        fonts.update(_dict_refs(doc, owner, "Resources/Font").values())
        xobjs = _dict_refs(doc, owner, "Resources/XObject")
        for name in set(DO.findall(content)):
            x = xobjs.get(name)
            if x and x not in seen and doc.xref_get_key(x, "Subtype")[1] == "/Form":
                seen.add(x)
                walk(x, doc.xref_stream(x).decode("latin1"))

    walk(page.xref, page.read_contents().decode("latin1"))
    return fonts, seen


# ---------- render ----------

def _rgb(c):
    return "#%02x%02x%02x" % tuple(int(v * 255) for v in c) if c else "none"


def _drawings(page):
    parts = []  # (paint order, svg)
    for dr in page.get_drawings():
        path, cur = [], None

        def move(pt):
            # Only start a new subpath when a segment doesn't continue the last one,
            # otherwise filled shapes (logos, barcodes) break into open strokes
            if cur is not None and abs(cur.x - pt.x) < 0.01 and abs(cur.y - pt.y) < 0.01:
                return ""
            return f"M{pt.x:.2f} {pt.y:.2f}"

        for it in dr["items"]:
            op = it[0]
            if op == "l":
                path.append(f"{move(it[1])}L{it[2].x:.2f} {it[2].y:.2f}")
                cur = it[2]
            elif op == "c":
                a, b, c, e = it[1:5]
                path.append(f"{move(a)}C{b.x:.2f} {b.y:.2f} {c.x:.2f} {c.y:.2f} {e.x:.2f} {e.y:.2f}")
                cur = e
            elif op == "re":
                r = it[1]
                path.append(f"M{r.x0:.2f} {r.y0:.2f}H{r.x1:.2f}V{r.y1:.2f}H{r.x0:.2f}Z")
                cur = None
            elif op == "qu":
                q = it[1]
                path.append(f"M{q.ul.x:.2f} {q.ul.y:.2f}L{q.ur.x:.2f} {q.ur.y:.2f}"
                            f"L{q.lr.x:.2f} {q.lr.y:.2f}L{q.ll.x:.2f} {q.ll.y:.2f}Z")
                cur = None
        if not path:
            continue
        if dr.get("closePath"):
            path.append("Z")
        stroke = _rgb(dr.get("color")) if dr["type"] in ("s", "fs") else "none"
        parts.append((dr["seqno"],
            f'<path d="{"".join(path)}" fill="{_rgb(dr.get("fill"))}" stroke="{stroke}" '
            f'fill-rule="{"evenodd" if dr.get("even_odd") else "nonzero"}" '
            f'stroke-width="{dr.get("width") or 1:.2f}" fill-opacity="{dr.get("fill_opacity") or 1}" '
            f'stroke-opacity="{dr.get("stroke_opacity") or 1}"/>'
        ))
    return parts


def _images(doc, page, st):
    # get_image_info lists images and shadings in paint order; pair them with the matching
    # bboxlog entries to learn where each sits relative to the vector drawings
    seqs = [i for i, (kind, _) in enumerate(page.get_bboxlog())
            if kind in ("fill-image", "fill-imgmask", "fill-shade")]
    parts = []
    for seq, info in zip(seqs, page.get_image_info(xrefs=True)):
        xref = info.get("xref")
        if not xref:
            st.shades_skipped += 1  # gradients: no SVG equivalent here, use --bg raster
            continue
        pix = pymupdf.Pixmap(doc, xref)
        if pix.n - pix.alpha > 3:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        r = info["bbox"]
        parts.append((seq,
            f'<image x="{r[0]:.2f}" y="{r[1]:.2f}" width="{r[2]-r[0]:.2f}" height="{r[3]-r[1]:.2f}" '
            f'preserveAspectRatio="none" href="data:image/png;base64,{base64.b64encode(pix.tobytes("png")).decode()}"/>'
        ))
    return parts


TR = re.compile(rb"\b[0-7]\s+Tr\b")
BT = re.compile(rb"\bBT\b")
_textless = {}


def _textless_page(doc, pno):
    """The page from a copy of the PDF where every text object draws invisibly (render mode 3)."""
    if id(doc) not in _textless:
        _textless[id(doc)] = (pymupdf.open("pdf", doc.tobytes()), set())
    copy, done = _textless[id(doc)]
    p = copy[pno]
    _, forms = page_resources(copy, p)
    for x in set(p.get_contents()) | forms:
        if x not in done:  # forms can be shared between pages; patch each stream once
            copy.update_stream(x, BT.sub(b"BT 3 Tr", TR.sub(b"3 Tr", copy.xref_stream(x))))
            done.add(x)
    return p


def _raster_background(doc, pno, dpi):
    """Everything except text, flattened to one image: shadings, patterns, clips, blends all come
    along for free. Text goes on top as real, selectable text."""
    pix = _textless_page(doc, pno).get_pixmap(dpi=dpi)
    r = doc[pno].rect
    jpg = base64.b64encode(pix.tobytes("jpg", jpg_quality=88)).decode()
    return (f'<image x="0" y="0" width="{r.width}" height="{r.height}" preserveAspectRatio="none" '
            f'href="data:image/jpeg;base64,{jpg}"/>')


_webfonts = {}


def _cached_webfont(doc, fx, sw, shift):
    key = (id(doc), fx, sw, shift)
    if key not in _webfonts:
        try:
            _webfonts[key] = to_webfont(doc, fx, sw, shift)
        except Exception as e:  # remember failures too; they won't change
            _webfonts[key] = e
    if isinstance(_webfonts[key], Exception):
        raise _webfonts[key]
    return _webfonts[key]


def _spread_ligatures(spans):
    """MuPDF splits a ligature glyph (ffi) into letters badly: the first letter gets the whole
    ligature's width and the rest are stacked at its end, on top of the next letter. Spread
    them evenly across the ligature's width instead."""
    for s in spans:
        ch = s["chars"]
        i = 1
        while i < len(ch):
            j = i
            while j + 1 < len(ch) and abs(ch[j + 1]["origin"][0] - ch[i]["origin"][0]) < 0.01:
                j += 1
            if j > i and ch[i - 1]["c"].strip():  # ch[i..j-1] are tails of ch[i-1]; ch[j] is real
                lead, parts = ch[i - 1], ch[i - 1:j]
                x0, x1 = lead["bbox"][0], lead["bbox"][2]
                w = (x1 - x0) / len(parts)
                for k, c in enumerate(parts):
                    c["origin"] = (lead["origin"][0] + k * w, c["origin"][1])
                    c["bbox"] = (x0 + k * w, c["bbox"][1], x0 + (k + 1) * w, c["bbox"][3])
            i = j + 1


def render_page(doc, pno, bg="raster", bg_dpi=192, editable=False, font_sink=None):
    """font_sink, if given, receives {css family: (kind, font bytes, codepoints)} for write-back."""
    page = doc[pno]
    st = Stats(page=pno + 1)
    W, H = page.rect.width, page.rect.height
    d = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    spans = [s for b in d["blocks"] if b["type"] == 0 for l in b["lines"] for s in l["spans"]]
    _spread_ligatures(spans)

    # chars each span-font needs; span font names are cut at 24 chars
    need, space = {}, {}
    for s in spans:
        need.setdefault(s["font"], Counter()).update(ord(c["c"]) for c in s["chars"] if ord(c["c"]) > 32)
        space.setdefault(s["font"], []).extend(
            (c["bbox"][2] - c["bbox"][0]) / s["size"] * 1000 for c in s["chars"] if c["c"] == " ")

    candidates = {}
    for fx in page_resources(doc, page)[0]:
        base = doc.xref_get_key(fx, "BaseFont")[1].lstrip("/").split("+")[-1]
        candidates.setdefault(base, []).append(fx)

    texts = {}
    for sp in spans:
        texts[sp["font"]] = texts.get(sp["font"], "") + "".join(c["c"] for c in sp["chars"])
    css, family, coverage, space_units = [], {}, {}, {}
    for name, chars in need.items():
        shift = glyph_order_shift(texts.get(name, ""))
        xrefs = candidates.get(name) or next(
            (v for k, v in sorted(candidates.items()) if k.startswith(name)), [])
        # One font name can come as many subsets (Chrome writes a fresh subset per text run).
        # Convert them all and chain the ones that add coverage, best first: the browser then
        # takes each char from the first subset that has it. Same design, so metrics match.
        sw = sorted(space[name])[len(space[name]) // 2] if space[name] else None
        space_units[name] = sw or 250
        subsets = []
        for fx in xrefs:
            try:
                kind, data, cov, repair = _cached_webfont(doc, fx, sw, shift)
            except Exception:
                continue
            fixed = Counter()
            for u, n in chars.items():
                fixed[ord(repair.get(chr(u), chr(u)))] += n
            subsets.append((kind, data, cov, fx, fixed, repair))
        chain, covered = [], set()
        for sub in sorted(subsets, key=lambda t: -sum(n for u, n in t[4].items() if u in t[2])):
            if not sub[4] or set(sub[4]) & sub[2] - covered:
                chain.append(sub)
                covered |= sub[2]
        total = sum(chars.values())
        missing = sum(n for u, n in chain[0][4].items() if u not in covered) if chain else total
        # Subsets missing letters draw blanks; the system font is the better bet then.
        # Happens when a non-embedded font and a tiny subset share a name (e.g. Helvetica).
        # A stray unmapped char or two is fine: the browser falls back for just those.
        if not chain or missing > 0.02 * total:
            st.fallback_fonts.append(name)
            continue
        repair = chain[0][5]
        if repair:
            st.repaired_fonts.append(name)
            for sp in spans:
                if sp["font"] == name:
                    for c in sp["chars"]:
                        c["c"] = repair.get(c["c"], c["c"])
        for kind, data, cov, fx, _, _ in chain:
            if font_sink is not None:
                font_sink[f"pdf{fx}"] = (kind, data, cov)
            css.append(f"@font-face{{font-family:pdf{fx};src:url(data:font/{kind};base64,"
                       f"{base64.b64encode(data).decode()})}}")
        family[name] = ",".join(f"pdf{sub[3]}" for sub in chain)
        coverage[name] = covered
    st.fonts_used, st.fonts_embedded = len(need), len(family)

    st.bg = bg
    if bg == "raster":
        parts = [_raster_background(doc, pno, bg_dpi)]
    else:
        drawn = _drawings(page)
        img = _images(doc, page, st)
        st.drawings, st.images = len(drawn), len(img)
        parts = [svg for _, svg in sorted(drawn + img, key=lambda t: t[0])]

    overlays = []
    # one <g> per column-wise piece of a block, so each can be swapped for its edit box
    pieces = [seg for b in d["blocks"] if b["type"] == 0 for seg in editor.segments(b)]
    for bi, b in enumerate(pieces):
        parts.append(f'<g id="b{bi}">')
        for l in b["lines"]:
            cos, sin = l["dir"]
            angle = -math.degrees(math.atan2(-sin, cos))  # PDF line direction -> SVG rotation
            st.rotated_lines += angle != 0
            for s in l["spans"]:
                chars = s["chars"]
                if not chars:
                    continue
                st.chars += len(chars)
                fam = family.get(s["font"])
                if fam:
                    st.glyphs_missing += sum(1 for c in chars if c["c"] != " " and ord(c["c"]) not in coverage[s["font"]])
                ox, oy = chars[0]["origin"]
                if angle == 0:
                    # pin every glyph at its PDF x, so spacing and justification match exactly
                    xs = " ".join("%.2f" % c["origin"][0] for c in chars)
                    pos = f'x="{xs}" y="{oy:.2f}"'
                else:
                    pos = f'x="{ox:.2f}" y="{oy:.2f}" transform="rotate({angle:.2f} {ox:.2f} {oy:.2f})"'
                weight = "bold" if not fam and "Bold" in s["font"] else "normal"
                ff = (fam + "," if fam else "") + fallback_for(s["font"])
                parts.append(
                    f'<text {pos} font-family="{ff}" font-size="{s["size"]:.2f}" '
                    f'font-weight="{weight}" fill="#{s["color"]:06x}" '
                    f'fill-opacity="{s.get("alpha", 255) / 255:.2f}" xml:space="preserve">'
                    f'{html.escape("".join(c["c"] for c in chars))}</text>'
                )
        parts.append("</g>")
        if editable:
            if editor.is_locked(b):
                st.locked_blocks += 1
            else:
                overlays.append(editor.edit_div(bi, b, family, fallback_for, space_units))

    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
           f'width="{W}pt" height="{H}pt">{"".join(parts)}</svg>')
    if not editable:
        page_html = (
            f'<!doctype html><meta charset="utf-8"><title>Page {pno + 1}</title>'
            f'<style>{"".join(css)}{NO_LIGATURES}body{{margin:0;background:#fff}}svg{{display:block}}</style>'
            f"<body>{svg}</body>"
        )
        return page_html, st
    st.edit_blocks = len(overlays)
    page_html = (
        f'<!doctype html><html lang="en"><meta charset="utf-8"><title>Page {pno + 1}</title>'
        f'<style>{"".join(css)}{NO_LIGATURES}{editor.css(W, H)}</style>'
        f'<body>{editor.BAR}<div id="pg">{svg}{"".join(overlays)}</div>'
        f"<script>{editor.JS}</script></body></html>"
    )
    return page_html, st


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--page", type=int, default=1, help="1-based page number")
    ap.add_argument("--out", required=True)
    ap.add_argument("--bg", choices=("raster", "vector"), default="raster")
    ap.add_argument("--editable", action="store_true", help="click-to-edit blocks, save as HTML or print to PDF")
    a = ap.parse_args()
    out, stats = render_page(pymupdf.open(a.pdf), a.page - 1, a.bg, editable=a.editable)
    open(a.out, "w").write(out)
    print(stats)
