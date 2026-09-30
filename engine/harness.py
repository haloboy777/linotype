"""Render PDF pages to HTML, screenshot them, and score them against the real page.

    uv run harness.py ~/Downloads/file.pdf --pages 1,2
    uv run harness.py ~/Downloads/file.pdf --pages 1-5 --open

Per page, writes to out/<pdf-stem>/pNNN/:
    page.html   the render
    ref.png     the PDF page rasterised by MuPDF (ground truth)
    html.png    Chrome's screenshot of page.html
    diff.png    red = pixels that differ
    side.png    ref | html | diff, for eyeballing
and prints one score row per page, plus report.json next to the page folders.
"""
import argparse
import html
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pymupdf
from PIL import Image, ImageChops, ImageFilter

from render import render_page

CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
DPI = 96          # CSS px are 1/96 in, so a 96-dpi raster lines up 1:1 with the screenshot
THRESHOLD = 64    # grey-level difference that counts as a real mismatch, not anti-aliasing


def parse_pages(spec, n):
    pages = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        pages.update(range(int(a), int(b or a) + 1))
    return [p for p in sorted(pages) if 1 <= p <= n]


BROWSER = "chrome"  # or "webkit": what Safari and the macOS desktop app (WKWebView) use
_webkit = None


def _webkit_page(w, h):
    global _webkit
    if _webkit is None:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        _webkit = pw.webkit.launch()
    return _webkit.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)


def screenshot(html_path, png_path, w, h, hash=""):
    if BROWSER == "webkit":
        page = _webkit_page(w, h)
        page.goto(html_path.resolve().as_uri() + hash)
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(300)
        page.screenshot(path=str(png_path))
        page.close()
        return
    subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
         f"--window-size={w},{h}", f"--screenshot={png_path}", html_path.resolve().as_uri() + hash],
        check=True, capture_output=True, timeout=120,
    )


def line_fit(html_path):
    """Share of edit boxes whose reflowed text takes the same number of lines as the PDF."""
    if BROWSER == "webkit":
        page = _webkit_page(1200, 2000)
        page.goto(html_path.resolve().as_uri() + "#reflow")
        page.wait_for_selector("#metrics", state="attached", timeout=60000)
        m = json.loads(page.eval_on_selector("#metrics", "e => e.textContent"))
        page.close()
        off = [x for x in m if x["got"] != x["want"]]
        return 100 * (1 - len(off) / max(len(m), 1)), off
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--virtual-time-budget=5000", "--window-size=1200,2000",
         "--dump-dom", html_path.resolve().as_uri() + "#reflow"],
        capture_output=True, text=True, timeout=120).stdout
    start = dom.find('<pre id="metrics" hidden="">')
    m = json.loads(html.unescape(dom[dom.index(">", start) + 1:dom.index("</pre>", start)]))
    off = [x for x in m if x["got"] != x["want"]]
    return 100 * (1 - len(off) / max(len(m), 1)), off


def score(ref_png, html_png, diff_png, side_png):
    ref = Image.open(ref_png).convert("RGB")
    got = Image.open(html_png).convert("RGB").crop((0, 0, *ref.size))
    # a 1px blur on both sides stops sub-pixel glyph placement from counting as a mismatch
    a = ref.convert("L").filter(ImageFilter.GaussianBlur(1))
    b = got.convert("L").filter(ImageFilter.GaussianBlur(1))
    delta = ImageChops.difference(a, b)
    mask = delta.point(lambda v: 255 if v > THRESHOLD else 0)
    hist = mask.histogram()
    mismatch = hist[255] / (ref.width * ref.height) * 100

    overlay = ref.copy()
    overlay.paste((230, 30, 30), mask=mask)
    Image.blend(ref, overlay, 0.7).save(diff_png)

    side = Image.new("RGB", (ref.width * 3, ref.height), "white")
    for i, im in enumerate((ref, got, Image.open(diff_png))):
        side.paste(im, (i * ref.width, 0))
    side.save(side_png)
    return mismatch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages", default="1")
    ap.add_argument("--out", default="out")
    ap.add_argument("--bg", choices=("raster", "vector"), default="raster",
                    help="raster: non-text content as one image (faithful); vector: SVG paths + images")
    ap.add_argument("--editable", action="store_true",
                    help="build the click-to-edit page and also score it with every block reflowed")
    ap.add_argument("--browser", choices=("chrome", "webkit"), default="chrome",
                    help="webkit = Safari / the macOS app's WKWebView (needs `playwright install webkit`)")
    ap.add_argument("--open", action="store_true", help="open each side.png when done")
    a = ap.parse_args()
    global BROWSER
    BROWSER = a.browser

    doc = pymupdf.open(a.pdf)
    root = Path(a.out) / Path(a.pdf).stem / (a.bg + ("-edit" if a.editable else "") + ("-webkit" if a.browser == "webkit" else ""))
    rows = []
    for pno in parse_pages(a.pages, len(doc)):
        d = root / f"p{pno:03d}"
        d.mkdir(parents=True, exist_ok=True)
        page_html, st = render_page(doc, pno - 1, a.bg, editable=a.editable)
        (d / "page.html").write_text(page_html)

        pix = doc[pno - 1].get_pixmap(dpi=DPI)
        pix.save(d / "ref.png")
        screenshot(d / "page.html", d / "html.png", pix.width, pix.height, "#view" if a.editable else "")
        mismatch = score(d / "ref.png", d / "html.png", d / "diff.png", d / "side.png")
        row = {**asdict(st), "mismatch_pct": round(mismatch, 2), "dir": str(d)}
        extra = ""
        if a.editable:
            # every block swapped to its editable box: how far does reflowed text drift?
            screenshot(d / "page.html", d / "reflow.png", pix.width, pix.height, "#reflow")
            rm = score(d / "ref.png", d / "reflow.png", d / "reflow_diff.png", d / "reflow_side.png")
            fit, off = line_fit(d / "page.html")
            row.update(reflow_mismatch_pct=round(rm, 2), line_fit_pct=round(fit, 1), line_misfits=off)
            extra = (f"reflow {rm:5.2f}%  same line count {fit:5.1f}%  "
                     f"editable {st.edit_blocks} locked {st.locked_blocks}  ")
        rows.append(row)
        print(f"p{pno:<3} mismatch {mismatch:5.2f}%  {extra}fonts {st.fonts_embedded}/{st.fonts_used}  "
              f"missing glyphs {st.glyphs_missing}  shades skipped {st.shades_skipped}  "
              f"fallback {st.fallback_fonts or '-'}  repaired {st.repaired_fonts or '-'}", flush=True)
        if a.open:
            subprocess.run(["open", d / "side.png"])

    (root / "report.json").write_text(json.dumps(rows, indent=2))
    print(f"report: {root / 'report.json'}")


if __name__ == "__main__":
    sys.exit(main())
