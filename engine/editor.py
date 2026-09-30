"""Editable overlay: each text block gets a reflowable contenteditable box that sits exactly
over its pinned SVG glyphs. Untouched blocks stay pixel-exact; clicking one swaps in the box."""
import html
import re
import statistics

# Stamps and legal notices stay as they are: rotated or translucent text, or notice wording
NOTICE = re.compile(r"non-commercial use|all rights reserved|reproduc", re.I)
ASCENT, DESCENT = 0.9, 0.25
# Scripts whose letters change shape with their neighbours (Indic, Arabic, Thai, ...). Their
# glyphs can't be placed one character at a time; words are shaped by the browser and on save.
SHAPED = re.compile("[\u0590-\u08ff\u0900-\u0dff\u0e00-\u0eff\u0f00-\u109f\u1780-\u17ff\ufb1d-\ufdff\ufe70-\ufeff]")
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
    fb = fallback(s["font"], "".join(c["c"] for c in s["chars"])) if callable(fallback) else fallback
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

    shaped = bool(SHAPED.search("".join(c["c"] for l in lines for s in l["spans"] for c in s["chars"])))
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
                if shaped and (k[1], k[2]) == (base[1], base[2]):
                    k = base  # one font per cluster, or the browser shapes it in two halves
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
                    elif not shaped and prev is not None and t != " " and prev["c"] != " " and \
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
        # blank space above this paragraph in the PDF, beyond normal line spacing
        above = baselines[p[0]] - baselines[p[0] - 1] - lh if p[0] else 0
        margin = f"margin-top:{above:.2f}pt;" if above > 0.3 * lh else ""
        out.append(f'<p style="{margin}text-indent:{indent:.2f}pt">{"".join(body)}</p>')

    if shaped:  # spacing measured from char positions means nothing once letters are shaped
        spacing = word_spacing = 0.0
    # one-line labels (datelines, tickers, kickers) stay on one line; wrapping them only ever hurts
    nowrap = "white-space:nowrap;" if len(lines) == 1 else ""
    # place the box so its first baseline lands on the PDF's first baseline
    top = baselines[0] - ((lh - (ASCENT + DESCENT) * size) / 2 + ASCENT * size)
    box_w = width * 1.02 + 1 if ragged else width + 1  # ragged lines may run to the very edge
    height = baselines[-1] - baselines[0] + lh
    return (
        f'<div class="ed{" cx" if shaped else ""}" data-b="{bi}" data-h="{height:.2f}" data-lines="{len(lines)}" '
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
.ed.new{{outline:1px solid #3b82f6;resize:horizontal;overflow:hidden;background:rgba(255,255,255,.001)}}
.ed.new:not(:focus){{outline:1px dashed #16a34a}}
.ed.deleted,.ed.deleted *{{color:transparent!important}}
.ed.deleted{{outline:1px dashed #dc2626!important;cursor:default;
     background:repeating-linear-gradient(135deg,rgba(220,38,38,.07) 0 6px,transparent 6px 12px)}}
body.adding #pg{{cursor:crosshair}}
body.adding .ed{{cursor:text}}
#chip{{position:absolute;z-index:10;display:flex;gap:2px;padding:2px;border-radius:6px;background:#111;
     transform:translate(-100%,calc(-100% - 3px));box-shadow:0 2px 8px rgba(0,0,0,.25)}}
#chip[hidden]{{display:none}}
#chip button{{font:12px/1 system-ui;border:0;background:transparent;color:#fff;width:22px;height:20px;
     border-radius:4px;cursor:pointer;padding:0}}
#chip button:hover{{background:#374151}}
#chip button[hidden]{{display:none}}
#chip [data-a=move]{{cursor:grab}}
@page{{size:{W}pt {H}pt;margin:0}}
@media print{{#bar,#chip{{display:none}}body{{background:#fff}}#pg{{margin:0}}
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
  if (ed.dataset.fit || ed.classList.contains('cx')) return;  // letter-spacing would break shaping
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
function count(restored) {
  const n = document.querySelectorAll('.ed.edited').length;
  const el = document.getElementById('n');
  if (el) el.textContent = n + ' edited';
  if (EMBED) parent.postMessage({type: 'edits', n, restored: !!restored}, '*');
}

// What each edited box holds, so a saved file can be reopened with its edits still editable
window.collectState = () => [...document.querySelectorAll('.ed.edited, .ed.deleted')].map(ed => ({
  b: ed.dataset.b, deleted: ed.classList.contains('deleted'), html: ed.innerHTML,
  isNew: ed.classList.contains('new'), style: ed.classList.contains('new') ? ed.getAttribute('style') : '',
  bbox: ed.dataset.bbox || '',
}));
window.restoreState = items => document.fonts.ready.then(() => {
  for (const it of items || []) {
    if (it.isNew) {
      const ed = document.createElement('div');
      ed.className = 'ed new on edited'; ed.dataset.b = it.b; ed.spellcheck = false;
      ed.setAttribute('style', it.style); ed.innerHTML = it.html;
      document.getElementById('pg').appendChild(ed); wire(ed); ed.dataset.orig = ed.innerHTML;
      continue;
    }
    const ed = document.querySelector(`.ed[data-b="${it.b}"]`);
    if (!ed || (it.bbox && ed.dataset.bbox !== it.bbox)) continue;  // page layout changed: skip
    if (it.deleted) { setDeleted(ed, true); continue; }
    activate(ed, false);          // remembers the original text, hides the exact glyphs
    ed.innerHTML = it.html;
    ed.classList.add('edited');
    checkOverflow(ed);
  }
  chip.hidden = true;
  count(true);
});

// What the app's Save sends back: every glyph of every edited block, where the browser put it.
// The server writes those glyphs into the original PDF, so the file matches what you saw.
window.collectEdits = () => {
  if (document.activeElement && document.activeElement.classList.contains('ed')) document.activeElement.blur();
  const pg = document.getElementById('pg').getBoundingClientRect();
  const pt = px => px * 0.75;
  return [...document.querySelectorAll('.ed.edited')].map(ed => {
    // an existing block's bbox is where its old text gets removed; a new box has none
    const bbox = ed.dataset.bbox ? ed.dataset.bbox.split(',').map(Number) : null;
    if (ed.classList.contains('deleted')) return {b: ed.dataset.b, bbox, glyphs: [], words: [], text: ''};
    const lh = parseFloat(ed.style.lineHeight);
    const shaped = ed.classList.contains('cx') || SHAPED_RE.test(ed.textContent);

    // 1. every character with its box and style, in text order
    const chars = [];
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
        if (box) chars.push({node, i, c: node.data[i], box, style});
      }
    }

    // 2. group into lines by vertical position
    let lineY = null, line = -1;
    for (const ch of chars) {
      const cy = pt((ch.box.top + ch.box.bottom) / 2 - pg.top);
      if (lineY === null || Math.abs(cy - lineY) > lh / 2) { line++; lineY = cy; ch.first = true; }
      ch.line = line;
    }

    // 3. each line's baseline, measured without touching the page: a character's box starts at
    //    baseline minus its font's ascent, and canvas reports that ascent for the same font.
    const ctx = (window._mctx ||= document.createElement('canvas').getContext('2d'));
    const asc = {};
    const ascent = el => {
      const cs = getComputedStyle(el);
      const key = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
      if (!(key in asc)) { ctx.font = key; asc[key] = ctx.measureText('x').fontBoundingBoxAscent; }
      return asc[key];
    };
    const cands = [];
    for (const ch of chars) (cands[ch.line] ||= []).push(pt(ch.box.top + ascent(ch.node.parentElement) - pg.top));
    const base = cands.map(ys => ys.sort((a, b) => a - b)[ys.length >> 1]);

    // 4. glyphs (placed one by one) or words (shaped scripts, placed whole)
    const glyphs = [], words = [];
    let prev = null, word = null;
    for (const ch of chars) {
      const x = pt(ch.box.left - pg.left), y = base[ch.line];
      if (shaped) {
        if (!ch.c.trim() || !word || ch.line !== word.line) {
          if (word && word.text) words.push(word);
          word = ch.c.trim() ? {...ch.style, fam: ch.style.fams[0], text: '', x, line: ch.line, y} : null;
        }
        if (word) { word.text += ch.c; word.x = Math.min(word.x, x); }
        continue;
      }
      // the browser's own hyphenation draws a hyphen that isn't in the text: add it back
      if (prev && ch.line > prev.line && /\w/.test(prev.c) && /\w/.test(ch.c))
        glyphs.push({...prev.style, c: '-', x: pt(prev.box.right - pg.left), y: base[prev.line]});
      prev = ch;
      if (ch.c.trim()) glyphs.push({...ch.style, c: ch.c, x, y});
    }
    if (word && word.text) words.push(word);
    return {b: ed.dataset.b, bbox, glyphs, words,
            text: shaped ? ed.innerText.replace(/\s+/g, ' ').trim() : ''};
  });
};

function checkOverflow(ed) {
  if (!ed.dataset.h) return;  // new boxes have no original space to overflow
  const extra = ed.scrollHeight - parseFloat(ed.dataset.h) * 4 / 3;
  const lines = Math.ceil(extra / (parseFloat(ed.style.lineHeight) * 4 / 3) - 0.3);
  ed.classList.toggle('over', lines > 0);
  ed.dataset.over = lines > 0 ? `+${lines} line${lines > 1 ? 's' : ''}` : '';
}

function activate(ed, focus) {
  if (!ed.dataset.orig) ed.dataset.orig = ed.innerHTML;
  ed.classList.add('on');
  const g = glyphs(ed);
  if (g) g.style.display = 'none';
  if (focus) { ed.contentEditable = 'true'; ed.focus(); }
}

// ---- the chip: ✕ / ↩ on the block under the mouse, plus ⠿ to move a new box ----
const PG = document.getElementById('pg');
const chip = document.createElement('div');
chip.id = 'chip'; chip.hidden = true;
chip.innerHTML = '<button data-a="move" title="Drag to move">⠿</button><button data-a="del"></button>';
PG.appendChild(chip);
let chipFor = null, hideTimer = null;
const ptOf = v => parseFloat(v) || 0;

function placeChip() {
  if (!chipFor) return;
  chip.style.left = (ptOf(chipFor.style.left) + chipFor.offsetWidth * 0.75) + 'pt';
  chip.style.top = ptOf(chipFor.style.top) + 'pt';
}
function showChip(ed) {
  if (!ed.isConnected) return;
  clearTimeout(hideTimer);
  chipFor = ed;
  const deleted = ed.classList.contains('deleted');
  chip.querySelector('[data-a=move]').hidden = !ed.classList.contains('new');
  const del = chip.querySelector('[data-a=del]');
  del.textContent = deleted ? '↩' : '✕';
  del.title = deleted ? 'Restore this block' : 'Delete this block';
  chip.hidden = false;
  placeChip();
}
function hideChipSoon() {
  clearTimeout(hideTimer);
  hideTimer = setTimeout(() => {
    // keep it while its block is focused or still under the mouse (no new mouseenter comes then)
    if (chipFor && chipFor.isConnected && (document.activeElement === chipFor || chipFor.matches(':hover'))) return;
    chip.hidden = true;
  }, 350);
}
chip.addEventListener('mouseenter', () => clearTimeout(hideTimer));
chip.addEventListener('mouseleave', hideChipSoon);
chip.addEventListener('mousedown', e => e.preventDefault());  // keep focus in the box being edited

function setDeleted(ed, on) {
  const g = glyphs(ed);
  if (ed.classList.contains('new')) {  // a box added in this session just goes away
    ed.remove(); chip.hidden = true; count(); return;
  }
  ed.blur();
  ed.contentEditable = 'false';
  if (on) {
    ed.classList.remove('on', 'over');
    ed.classList.add('deleted', 'edited');
    if (g) { g.style.display = ''; g.style.opacity = '.15'; }
  } else {
    ed.classList.remove('deleted', 'edited', 'on', 'over');
    if (ed.dataset.orig) ed.innerHTML = ed.dataset.orig;
    if (g) { g.style.display = ''; g.style.opacity = ''; }
  }
  showChip(ed);
  count();
}
chip.querySelector('[data-a=del]').addEventListener('click', () => {
  if (chipFor) setDeleted(chipFor, !chipFor.classList.contains('deleted'));
});

// drag a new box by its ⠿ handle
chip.querySelector('[data-a=move]').addEventListener('pointerdown', e => {
  const ed = chipFor;
  if (!ed) return;
  e.preventDefault();
  const h = e.currentTarget;
  h.setPointerCapture(e.pointerId);
  const sx = e.clientX, sy = e.clientY, l0 = ptOf(ed.style.left), t0 = ptOf(ed.style.top);
  const move = m => {
    ed.style.left = (l0 + (m.clientX - sx) * 0.75) + 'pt';
    ed.style.top = (t0 + (m.clientY - sy) * 0.75) + 'pt';
    placeChip();
  };
  const up = () => {
    h.removeEventListener('pointermove', move); h.removeEventListener('pointerup', up);
    if (ed.textContent.trim()) { ed.classList.add('edited'); count(); }
  };
  h.addEventListener('pointermove', move); h.addEventListener('pointerup', up);
});

const SHAPED_RE = /[֐-ࣿऀ-෿฀-໿ༀ-႟ក-៿יִ-﷿ﹰ-﻿]/;

function wire(ed) {
  ed.addEventListener('mousedown', e => {
    if (ed.classList.contains('deleted')) { e.preventDefault(); return; }  // restore with ↩
    if (ed.isContentEditable) return;
    e.preventDefault();
    activate(ed, true);
    const r = document.caretRangeFromPoint(e.clientX, e.clientY);
    if (r) { const s = getSelection(); s.removeAllRanges(); s.addRange(r); }
  });
  ed.addEventListener('mouseenter', () => showChip(ed));
  ed.addEventListener('mouseleave', hideChipSoon);
  ed.addEventListener('focus', () => showChip(ed));
  ed.addEventListener('input', () => {
    checkOverflow(ed);
    if (ed.classList.contains('new')) ed.classList.toggle('cx', SHAPED_RE.test(ed.textContent));
    placeChip();
  });
  ed.addEventListener('keydown', e => { if (e.key === 'Escape') ed.blur(); });
  ed.addEventListener('blur', () => {
    ed.contentEditable = 'false';
    if (ed.classList.contains('new')) {
      if (!ed.textContent.trim()) { ed.remove(); chip.hidden = true; }  // empty new box: gone
      else ed.classList.add('edited');
    } else if (ed.innerHTML === ed.dataset.orig) {   // untouched: back to the exact glyphs
      ed.classList.remove('on', 'over');
      const g = glyphs(ed);
      if (g) g.style.display = '';
    } else ed.classList.add('edited');
    hideChipSoon();
    count();
  });
}
eds.forEach(wire);

// ---- adding text: the app's "+ Text" turns this on, the next click on the page places a box ----
let addMode = false, newId = 0;
window.setAddMode = on => { addMode = on; document.body.classList.toggle('adding', on); };

function nearestBlock(x, y) {
  let best = null, bd = Infinity;
  for (const ed of document.querySelectorAll('.ed:not(.new)')) {
    const l = ptOf(ed.style.left), t = ptOf(ed.style.top);
    const w = ed.offsetWidth * 0.75, h = ed.offsetHeight * 0.75;
    const dx = Math.max(l - x, 0, x - (l + w)), dy = Math.max(t - y, 0, y - (t + h));
    if (dx * dx + dy * dy < bd) { bd = dx * dx + dy * dy; best = ed; }
  }
  return best;
}

function newBox(x, y) {
  // look like the text nearest to where you clicked: same font chain, size, color, leading
  const src = nearestBlock(x, y), s = src && src.style;
  const size = s ? ptOf(s.fontSize) : 11, lh = s ? ptOf(s.lineHeight) || size * 1.2 : size * 1.2;
  const pageW = PG.offsetWidth * 0.75;
  const ed = document.createElement('div');
  ed.className = 'ed new on';
  ed.dataset.b = 'n' + (++newId);
  ed.spellcheck = false;
  ed.style.cssText = `left:${x.toFixed(2)}pt;top:${(y - lh / 2).toFixed(2)}pt;` +
    `width:${Math.max(60, Math.min(240, pageW - x - 18)).toFixed(2)}pt;min-height:${lh}pt;line-height:${lh}pt;` +
    `font-size:${size}pt;font-family:${s ? s.fontFamily : 'Helvetica,Arial,sans-serif'};` +
    `color:${s ? s.color : '#000'};font-weight:${s ? s.fontWeight || 'normal' : 'normal'};text-align:left`;
  ed.innerHTML = '<p><br></p>';
  PG.appendChild(ed);
  wire(ed);
  ed.dataset.orig = ed.innerHTML;
  ed.contentEditable = 'true';
  ed.focus();
  showChip(ed);
}

PG.addEventListener('mousedown', e => {
  if (!addMode || e.target.closest('#chip')) return;
  const done = () => { setAddMode(false); if (EMBED) parent.postMessage({type: 'addDone'}, '*'); };
  if (e.target.closest('.ed')) { done(); return; }  // clicked existing text: just edit it
  e.preventDefault();
  const r = PG.getBoundingClientRect();
  newBox((e.clientX - r.left) * 0.75, (e.clientY - r.top) * 0.75);
  done();
}, true);
addEventListener('keydown', e => {
  if (e.key === 'Escape' && addMode) { setAddMode(false); if (EMBED) parent.postMessage({type: 'addDone'}, '*'); }
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
