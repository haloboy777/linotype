"""Editable overlay: each text block gets a reflowable contenteditable box that sits exactly
over its pinned SVG glyphs. Untouched blocks stay pixel-exact; clicking one swaps in the box."""
import html
import re
import statistics

# Stamps and legal notices stay as they are: rotated or translucent text, or notice wording
NOTICE = re.compile(r"non-commercial use|all rights reserved|reproduc", re.I)
ASCENT, DESCENT = 0.9, 0.25
# which word gap (as a quantile of all gaps in a justified block) the browser should start from;
# tuned with `harness.py --editable` against line counts
TIGHT_Q = 0.2  # the metrics cff_to_otf writes into every converted font


def is_locked(block):
    text = "".join(c["c"] for l in block["lines"] for s in l["spans"] for c in s["chars"])
    return (
        any(l["dir"] != (1.0, 0.0) for l in block["lines"])
        or any(s.get("alpha", 255) < 255 for l in block["lines"] for s in l["spans"])
        or bool(NOTICE.search(text))
    )


def _style(s, family, fallback):
    fam = family.get(s["font"])
    fb = fallback(s["font"]) if callable(fallback) else fallback
    # a char the subsets lack falls back to a system font of the same style, not the browser default
    return (f"{fam},{fb}" if fam else fb, round(s["size"], 2), s["color"],
            "bold" if not fam and "Bold" in s["font"] else "normal")


def _union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _baseline(l):
    return next((s["chars"][0]["origin"][1] for s in l["spans"] if s["chars"]), l["bbox"][3])


def segments(block):
    """Split a text block into column-wise pieces, each with one row per baseline.

    PyMuPDF sometimes puts side-by-side headlines into one block (split them: their x-ranges
    never overlap), and sometimes splits one widely justified line into word fragments
    (rejoin them: they sit on one baseline inside the column's x-range)."""
    cols = [[l["bbox"][0], l["bbox"][2], [l]] for l in block["lines"]]
    merged = True
    while merged:  # merge until no two columns overlap horizontally
        merged = False
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                a, b = cols[i], cols[j]
                if min(a[1], b[1]) - max(a[0], b[0]) > 0:
                    a[0], a[1], a[2] = min(a[0], b[0]), max(a[1], b[1]), a[2] + b[2]
                    del cols[j]
                    merged = True
                    break
            if merged:
                break

    out = []
    for _, _, lines in sorted(cols, key=lambda c: c[0]):
        rows = []
        for l in sorted(lines, key=lambda l: (_baseline(l), l["bbox"][0])):
            size = max((s["size"] for s in l["spans"]), default=10)
            if rows and rows[-1]["dir"] == l["dir"] and abs(_baseline(l) - rows[-1]["y"]) < 0.3 * size:
                r = rows[-1]
                r["spans"] += l["spans"]
                r["bbox"] = _union([r["bbox"], l["bbox"]])
            else:
                rows.append({"spans": list(l["spans"]), "bbox": l["bbox"], "dir": l["dir"], "y": _baseline(l)})
        out.append({"lines": rows, "bbox": _union([r["bbox"] for r in rows])})
    return out


def edit_div(bi, block, family, fallback, space_units):
    # lines with visible text only; Word leaves blocks that are nothing but a space
    lines = [l for l in block["lines"] if any(c["c"].strip() for s in l["spans"] for c in s["chars"])]
    if not lines:
        return ""
    bx0, _, bx1, _ = block["bbox"]

    # dominant style (by character count) becomes the box default; runs only override differences
    weight = {}
    for l in lines:
        for s in l["spans"]:
            k = _style(s, family, fallback)
            weight[k] = weight.get(k, 0) + len(s["chars"])
    base = max(weight, key=weight.get)
    size = base[1]

    baselines = [next(s["chars"][0]["origin"][1] for s in l["spans"] if s["chars"]) for l in lines]
    deltas = [b - a for a, b in zip(baselines, baselines[1:]) if 0 < b - a < 3 * size]
    lh = statistics.median(deltas) if deltas else size * 1.2

    xs = [l["bbox"][0] for l in lines]
    rgap = [bx1 - l["bbox"][2] for l in lines]
    centered = any(x - bx0 > 2 for x in xs) and all(
        abs((x - bx0) - r) < max(2, 0.05 * (bx1 - bx0)) for x, r in zip(xs, rgap))
    # The text's left edge is where most lines start. A first line left of it is a hanging
    # bullet, a first line right of it a paragraph indent; both become text-indent.
    left = bx0 if centered or len(lines) == 1 else min(xs[1:])
    width = bx1 - left
    lgap = [x - left for x in xs]
    # Tracking: how much tighter (or looser) the PDF sets letters than the font's own advances.
    # Mean, not median: most pairs sit at 0 and a few kerned pairs carry the whole difference.
    # Anything clearly positive is a word break drawn without a space char, not tracking.
    track = []
    for l in lines:
        for s in l["spans"]:
            ch = s["chars"]
            for a, b in zip(ch, ch[1:]):
                if a["c"] != " " and b["c"] != " ":
                    t = b["origin"][0] - a["origin"][0] - (a["bbox"][2] - a["bbox"][0])
                    if -0.5 * size < t < 0.06 * size:
                        track.append(t)
    spacing = statistics.fmean(track) if track else 0.0
    word_gap = spacing + 0.08 * size  # a gap wider than this between two letters is a word break

    # Word gaps as the PDF drew them, versus the space glyph + tracking the browser will use.
    gaps = []
    for l in lines:
        ink = [c for s in l["spans"] for c in s["chars"] if c["c"].strip()]
        gaps += [g for a, b in zip(ink, ink[1:]) if (g := b["origin"][0] - a["bbox"][2]) > word_gap]
    font = max(((s["font"], len(s["chars"])) for l in lines for s in l["spans"]), key=lambda t: t[1])[0]
    space_pt = space_units.get(font, 250) / 1000 * size

    def first_word(l):
        ink = [c for s in l["spans"] for c in s["chars"] if c["c"].strip()]
        if not ink:
            return 0.0
        end = ink[0]
        for a, b in zip(ink, ink[1:]):
            if b["origin"][0] - a["bbox"][2] > word_gap:
                break
            end = b
        return end["bbox"][2] - ink[0]["bbox"][0]

    # A line ends by choice when the next line's first word would have fit on it: the end of a
    # paragraph, a list item, a table cell. Lines that end because the next word didn't fit are
    # wraps, and those are what the browser is allowed to redo.
    forced = [not centered and bx1 - lines[i]["bbox"][2] > space_pt + first_word(lines[i + 1]) + 0.5
              for i in range(len(lines) - 1)] + [True]

    # paragraphs: after a deliberate break, at a first-line indent, or at a big vertical gap
    paras = [[0]]
    for i in range(1, len(lines)):
        gap = baselines[i] - baselines[i - 1]
        indented = not centered and lgap[i] > 0.4 * size
        if forced[i - 1] or indented or gap > 1.6 * lh or gap < 0:
            paras.append([])
        paras[-1].append(i)

    # justified if the lines that wrap (not those that end a paragraph) run to the right edge
    wraps = [i for p in paras for i in p[:-1]]
    flush = [rgap[i] < 1.5 for i in wraps]
    align = "center" if centered else "justify" if flush and sum(flush) / len(flush) >= 0.6 else "left"

    word_spacing = 0.0
    if gaps and align == "justify":
        # the layout program squeezes word gaps to fit more per line; browsers never go below a
        # normal space unless told to. Start from the tightest gaps the PDF used; justify stretches.
        tight = sorted(gaps)[int(len(gaps) * TIGHT_Q)]
        word_spacing = max(min(tight - space_pt - 2 * spacing, 0), -0.5 * space_pt)
    elif gaps:
        word_spacing = statistics.median(gaps) - space_pt - 2 * spacing

    # Centered text (headlines, captions) keeps the PDF's line breaks: someone balanced them.
    # Everything else reflows its wraps; deliberate breaks are already paragraph ends.
    ragged = align == "center"
    seen = set()  # text drawn twice (fill + outline) shows up as duplicate chars at one spot
    out = []
    for p in paras:
        runs = []  # [style, text]
        for n, i in enumerate(p):
            prev = None  # previous char on this line, to spot word gaps the PDF left without a space
            for s in lines[i]["spans"]:
                k = _style(s, family, fallback)
                for c in s["chars"]:
                    t = c["c"]
                    key = (t, round(c["origin"][0], 1), round(c["origin"][1], 1))
                    if key in seen or (ord(t) < 32 and t != " "):
                        continue
                    seen.add(key)
                    if prev is None and n and runs:
                        last = runs[-1][1]
                        if ragged:
                            runs[-1][1] = last.rstrip(" ") + "\n"
                        # line-end hyphen before a lowercase letter is a break, not a real hyphen
                        elif last.endswith("-") and t.islower():
                            runs[-1][1] = last[:-1]
                        elif not last.endswith(" ") and t != " ":
                            runs[-1][1] = last + " "
                    elif prev is not None and t != " " and prev["c"] != " " and \
                            c["origin"][0] - prev["bbox"][2] > word_gap:
                        t = " " + t
                    prev = c
                    if runs and runs[-1][0] == k:
                        runs[-1][1] += t
                    else:
                        runs.append([k, t])
        body = []
        for k, t in runs:
            t = html.escape(t).replace("\n", "<br>")
            if k == base:
                body.append(t)
            else:
                body.append(f'<span style="font-family:{k[0]};font-size:{k[1]}pt;color:#{k[2]:06x};'
                            f'font-weight:{k[3]}">{t}</span>')
        g = lgap[p[0]]
        indent = g if not centered and (g > 0.4 * size or g < -0.2 * size) else 0
        out.append(f'<p style="text-indent:{indent:.2f}pt">{"".join(body)}</p>')

    # one-line labels (datelines, tickers, kickers) stay on one line; wrapping them only ever hurts
    nowrap = "white-space:nowrap;" if len(lines) == 1 else ""
    # place the box so its first baseline lands on the PDF's first baseline
    top = baselines[0] - ((lh - (ASCENT + DESCENT) * size) / 2 + ASCENT * size)
    box_w = width * 1.02 + 1 if ragged else width + 1  # ragged lines may run to the very edge
    height = baselines[-1] - baselines[0] + lh
    return (
        f'<div class="ed" data-b="{bi}" data-h="{height:.2f}" data-lines="{len(lines)}" '
        f'data-bbox="{",".join(f"{v:.2f}" for v in block["bbox"])}" data-base="{baselines[0]:.2f}" spellcheck="false" style="left:{left:.2f}pt;'
        f'top:{top:.2f}pt;width:{box_w:.2f}pt;min-height:{height:.2f}pt;line-height:{lh:.2f}pt;'
        f'text-align:{align};font-family:{base[0]};font-size:{size}pt;color:#{base[2]:06x};{nowrap}'
        f'font-weight:{base[3]};letter-spacing:{spacing:.3f}pt;'
        f'word-spacing:{word_spacing:.3f}pt">{"".join(out)}</div>'
    )


def css(W, H):
    return f"""
body{{margin:0;background:#e5e5e5}}
#pg{{position:relative;width:{W}pt;height:{H}pt;margin:0 auto;background:#fff}}
#pg svg{{position:absolute;left:0;top:0}}
.ed{{position:absolute;cursor:text;outline:none;white-space:normal;hyphens:auto;-webkit-hyphens:auto;
     overflow-wrap:break-word;box-sizing:border-box}}
.ed p{{margin:0}}
.ed:not(.on),.ed:not(.on) *{{color:transparent!important}}
.ed:not(.on):hover,body.show .ed{{outline:1px dashed #3b82f6}}
.ed.on{{outline:1px solid #3b82f6;background:rgba(255,255,255,.001)}}
.ed.edited:not(:focus){{outline:1px dotted #16a34a}}
.ed.over{{outline:2px solid #e11d48!important}}
.ed.over::after{{content:attr(data-over);position:absolute;right:0;bottom:-16px;font:600 10px system-ui;
     color:#fff;background:#e11d48;padding:1px 4px;border-radius:3px}}
#bar{{position:fixed;top:12px;right:12px;z-index:9;display:flex;gap:6px;align-items:center;
     font:13px system-ui;background:#111;color:#fff;padding:6px 8px;border-radius:8px}}
#bar button{{font:inherit;border:0;border-radius:5px;padding:4px 8px;cursor:pointer}}
@page{{size:{W}pt {H}pt;margin:0}}
@media print{{#bar{{display:none}}body{{background:#fff}}#pg{{margin:0}}
     .ed,.ed.on,.ed.edited,.ed.over{{outline:none!important}}.ed.over::after{{display:none}}
     *{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}}}
"""


JS = r"""
const eds = [...document.querySelectorAll('.ed')];

function lineCount(ed) {
  // measure text nodes only: a range over the whole box also returns each <p>'s own rect,
  // which spans several lines and glues them into one cluster
  const lh = parseFloat(ed.style.lineHeight), ys = [];
  const walk = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    const r = document.createRange(); r.selectNodeContents(n);
    for (const x of r.getClientRects()) if (x.width > 0.5) ys.push((x.top + x.bottom) / 2 * 3 / 4);
  }
  ys.sort((a, b) => a - b);
  return ys.length && 1 + ys.slice(1).filter((y, i) => y - ys[i] > lh / 2).length;
}

// Nudge letter-spacing until the reflowed block has the PDF's line count. The estimate from
// glyph positions gets close; this closes the rest (the PDF's own justification engine differs).
function fit(ed) {
  if (ed.dataset.fit) return;
  ed.dataset.fit = '1';
  const want = +ed.dataset.lines;
  const start = parseFloat(ed.style.letterSpacing) || 0;
  let best = [Math.abs(lineCount(ed) - want), start];
  for (let step = 1; step <= 12 && best[0]; step++) {
    for (const ls of [start - 0.025 * step, start + 0.025 * step]) {
      ed.style.letterSpacing = ls.toFixed(3) + 'pt';
      const miss = Math.abs(lineCount(ed) - want);
      if (miss < best[0]) best = [miss, ls];
    }
  }
  ed.style.letterSpacing = best[1].toFixed(3) + 'pt';
}
document.fonts.ready.then(() => eds.forEach(fit));
const glyphs = ed => document.getElementById('b' + ed.dataset.b);
const EMBED = new URLSearchParams(location.search).has('embed');
function count() {
  const n = document.querySelectorAll('.ed.edited').length;
  const el = document.getElementById('n');
  if (el) el.textContent = n + ' edited';
  if (EMBED) parent.postMessage({type: 'edits', n}, '*');
}

// What the app's Save sends back: every glyph of every edited block, where the browser put it.
// The server writes those glyphs into the original PDF, so the file matches what you saw.
window.collectEdits = () => {
  if (document.activeElement && document.activeElement.classList.contains('ed')) document.activeElement.blur();
  const pg = document.getElementById('pg').getBoundingClientRect();
  const pt = px => px * 0.75;
  return [...document.querySelectorAll('.ed.edited')].map(ed => {
    const top = parseFloat(ed.style.top), lh = parseFloat(ed.style.lineHeight), base = parseFloat(ed.dataset.base);
    const glyphs = [];
    let prev = null;
    const walk = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT);
    for (let node = walk.nextNode(); node; node = walk.nextNode()) {
      const cs = getComputedStyle(node.parentElement);
      const style = {
        fams: cs.fontFamily.split(',').map(f => f.trim().replace(/["']/g, '')),
        size: pt(parseFloat(cs.fontSize)),
        color: (cs.color.match(/[\d.]+/g) || [0, 0, 0]).slice(0, 3).map(v => +v / 255),
        bold: parseInt(cs.fontWeight) >= 600,
      };
      for (let i = 0; i < node.length; i++) {
        const r = document.createRange(); r.setStart(node, i); r.setEnd(node, i + 1);
        const box = r.getClientRects()[0];
        if (!box) continue;
        const y = pt((box.top + box.bottom) / 2 - pg.top);
        const line = Math.max(0, Math.round((y - top - lh / 2) / lh));
        const c = node.data[i];
        // the browser's own hyphenation draws a hyphen that isn't in the text: add it back
        if (prev && line > prev.line && /\w/.test(prev.c) && /\w/.test(c))
          glyphs.push({...prev.style, c: '-', x: prev.right, y: base + prev.line * lh});
        prev = {c, line, right: pt(box.right - pg.left), style};
        if (!c.trim()) continue;
        glyphs.push({...style, c, x: pt(box.left - pg.left), y: base + line * lh});
      }
    }
    return {b: ed.dataset.b, bbox: ed.dataset.bbox.split(',').map(Number), glyphs};
  });
};

function checkOverflow(ed) {
  const extra = ed.scrollHeight - parseFloat(ed.dataset.h) * 4 / 3;
  const lines = Math.ceil(extra / (parseFloat(ed.style.lineHeight) * 4 / 3) - 0.3);
  ed.classList.toggle('over', lines > 0);
  ed.dataset.over = lines > 0 ? `+${lines} line${lines > 1 ? 's' : ''}` : '';
}

function activate(ed, focus) {
  if (!ed.dataset.orig) ed.dataset.orig = ed.innerHTML;
  ed.classList.add('on');
  glyphs(ed).style.display = 'none';
  if (focus) { ed.contentEditable = 'true'; ed.focus(); }
}

eds.forEach(ed => {
  ed.addEventListener('mousedown', e => {
    if (ed.isContentEditable) return;
    e.preventDefault();
    activate(ed, true);
    const r = document.caretRangeFromPoint(e.clientX, e.clientY);
    if (r) { const s = getSelection(); s.removeAllRanges(); s.addRange(r); }
  });
  ed.addEventListener('input', () => checkOverflow(ed));
  ed.addEventListener('keydown', e => { if (e.key === 'Escape') ed.blur(); });
  ed.addEventListener('blur', () => {
    ed.contentEditable = 'false';
    if (ed.innerHTML === ed.dataset.orig) {   // untouched: back to the exact glyphs
      ed.classList.remove('on', 'over');
      glyphs(ed).style.display = '';
    } else ed.classList.add('edited');
    count();
  });
});

document.getElementById('show').onclick = () => document.body.classList.toggle('show');
document.getElementById('print').onclick = () => print();
document.getElementById('save').onclick = () => {
  const doc = document.documentElement.cloneNode(true);
  doc.querySelectorAll('[contenteditable]').forEach(e => e.removeAttribute('contenteditable'));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob(['<!doctype html>' + doc.outerHTML], {type: 'text/html'}));
  a.download = (document.title || 'page').replace(/\W+/g, '_') + '_edited.html';
  a.click();
};
// #reflow: switch every block to its reflowed box (the harness scores how close that stays)
if (location.hash === '#reflow') document.fonts.ready.then(() => {
  eds.forEach(fit);
  eds.forEach(ed => activate(ed, false));
  // per-block line counts, read by the harness via --dump-dom
  const m = eds.map(ed => ({b: ed.dataset.b, want: +ed.dataset.lines, got: lineCount(ed),
                            text: ed.textContent.slice(0, 50)}));
  const pre = document.createElement('pre');
  pre.id = 'metrics'; pre.hidden = true; pre.textContent = JSON.stringify(m);
  document.body.appendChild(pre);
});
if (location.hash || EMBED) document.getElementById('bar').remove();  // screenshots, or inside the app
count();
"""

BAR = ('<div id="bar"><span id="n"></span><button id="show">Blocks</button>'
       '<button id="save">Save HTML</button><button id="print">Print / PDF</button></div>')
