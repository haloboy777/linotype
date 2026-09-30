"""Linotype engine: drop a PDF, edit it in the browser, save back to PDF.

    uv run app.py              # opens a drop page
    uv run app.py file.pdf     # opens straight into the editor; saves next to the file

Local mode (default): 127.0.0.1 only, one user, saves to disk. desktop.py shows the same
pages in a native window.

Server mode (LINOTYPE_MODE=server) for running behind a reverse proxy:
    LINOTYPE_PASSWORD    required to use it (login page, 30-day cookie)
    HOST / PORT          where to listen (default 127.0.0.1 / 8091)
    LINOTYPE_MAX_MB      largest PDF accepted (default 50)
    LINOTYPE_IDLE_MIN    minutes before an idle document is dropped (default 30)
    LINOTYPE_MAX_DOCS    open documents kept at most, oldest idle dropped first (default 30)
Each visitor's PDFs and added fonts live in memory only, never on disk; Save downloads the file.
"""
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import subprocess
import sys
import tarfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import pymupdf

import writeback
import render
from render import font_report, load_user_font, render_page

SERVER = os.environ.get("LINOTYPE_MODE", "local") == "server"
PASSWORD = os.environ.get("LINOTYPE_PASSWORD", "") if SERVER else ""
MAX_MB = int(os.environ.get("LINOTYPE_MAX_MB", "50" if SERVER else "300"))
MAX_UPLOAD = MAX_MB * 1024 * 1024
IDLE = int(os.environ.get("LINOTYPE_IDLE_MIN", "30")) * 60
MAX_DOCS = int(os.environ.get("LINOTYPE_MAX_DOCS", "30"))
# a stable login cookie: derived from the password, so it survives restarts and dies with it
COOKIE = hmac.new(PASSWORD.encode(), b"linotype-login-v1", hashlib.sha256).hexdigest() if PASSWORD else ""
# Devanagari font for Hindi the PDF's own font can't shape. In server mode the same file is
# given to the browser, so what you see while editing and what gets saved use one font.
SHAPED_FONT = "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"
if SERVER and os.path.exists(SHAPED_FONT):
    render.SHAPED_FALLBACK = "'Linotype Devanagari'," + render.SHAPED_FALLBACK
    render.EXTRA_CSS = "@font-face{font-family:'Linotype Devanagari';src:url(/fonts/devanagari.ttf)}"

sessions = {}
# Full font files the user added, by PDF font name: {name: (kind, bytes, codepoints, id, filename)}.
# Local mode: shared by every open PDF while the app runs. Server mode: per document (see
# Session.user_fonts), so one visitor's fonts never touch another's files. Never written to disk.
user_fonts = {}


def font_store(s):
    return s.user_fonts if SERVER else user_fonts


def font_overrides(s):
    return {name: f[:4] for name, f in font_store(s).items()}


ORIGINAL, STATE = "linotype-original.pdf", "linotype-state.json"


class Session:
    def __init__(self, name, data, src=None):
        self.name, self.data, self.src = name, data, src
        self.doc = pymupdf.open("pdf", data)
        self.restore, self.has_original = [], False
        # A file Linotype saved with "Keep original": edit the original again, with the saved
        # edits restored as live boxes, instead of editing the edited result
        if not self.doc.needs_pass and ORIGINAL in self.doc.embfile_names() and STATE in self.doc.embfile_names():
            try:
                original = self.doc.embfile_get(ORIGINAL)
                state = json.loads(self.doc.embfile_get(STATE))
                self.doc = pymupdf.open("pdf", original)
                self.data, self.restore, self.has_original = original, state.get("pages", []), True
            except Exception:
                self.doc = pymupdf.open("pdf", data)  # damaged attachment: edit the file as it is
        if self.doc.needs_pass:
            raise ValueError("This PDF is password-protected. Remove the password in your PDF viewer "
                             "(or with the tool that made it), then open it again.")
        self.lock = threading.Lock()  # a MuPDF document isn't safe to use from two threads
        self.pages = {}
        self.fonts = {}
        self.user_fonts = {}
        self.saved = None  # last file written, for "Show in Finder"
        self.last = time.time()

    def page_html(self, n):
        with self.lock:
            if n not in self.pages:
                self.pages[n], _ = render_page(self.doc, n, editable=True, font_sink=self.fonts,
                                               overrides=font_overrides(self))
            return self.pages[n]

    def out_path(self, download=False):
        stem = re.sub(r"-edited(-\d+)?$", "", Path(self.name).stem)
        if download:
            return f"{stem}-edited.pdf"
        folder = Path(self.src).parent if self.src else Path.home() / "Downloads"
        path, i = folder / f"{stem}-edited.pdf", 2
        while path.exists():
            path, i = folder / f"{stem}-edited-{i}.pdf", i + 1
        return path


def new_session(name, data, src=None):
    if SERVER:
        while len(sessions) >= MAX_DOCS:  # full: drop the one idle longest
            sessions.pop(min(sessions, key=lambda k: sessions[k].last), None)
    sid = secrets.token_urlsafe(16)
    sessions[sid] = Session(name, data, src)
    return sid


def expire_sessions():
    """Server mode: forget documents nobody has touched for IDLE seconds."""
    while True:
        time.sleep(60)
        cutoff = time.time() - IDLE
        for sid in [k for k, s in list(sessions.items()) if s.last < cutoff]:
            sessions.pop(sid, None)


def source_tarball():
    """AGPL §13: people using Linotype over a network can download the code that runs it."""
    here = Path(__file__).resolve().parent
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(here.glob("*.py")) + [here / n for n in ("pyproject.toml", "uv.lock", "linotype.spec", "build_app.sh")]:
            if p.exists():
                tar.add(p, arcname=f"linotype/engine/{p.name}")
        for p in (here.parent / "README.md", here.parent / "LICENSE", here / "LICENSE"):
            if p.exists():
                tar.add(p, arcname=f"linotype/{p.name}")
    return buf.getvalue()


def open_file(path):
    if os.environ.get("NO_OPEN") or SERVER:
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606 - opening the user's own file with their viewer
    else:
        subprocess.Popen(["xdg-open", path])


def attach_original(pdf, original, state):
    """Put the untouched original and the editor state inside the saved file, so Linotype can
    reopen it with every edit still editable, or revert. Anyone with the file can extract them."""
    doc = pymupdf.open("pdf", pdf)
    for name in (ORIGINAL, STATE):
        if name in doc.embfile_names():
            doc.embfile_del(name)
    doc.embfile_add(ORIGINAL, original, filename=ORIGINAL,
                    desc="The PDF before it was edited in Linotype (lets Linotype revert)")
    doc.embfile_add(STATE, json.dumps({"version": 1, "pages": state}).encode(), filename=STATE,
                    desc="Linotype's record of the edits, so they stay editable")
    return doc.tobytes(garbage=3, deflate=True)


def reveal_file(path):
    """Show the file selected in the system file manager."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    elif sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", path])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="text/html; charset=utf-8", code=200, headers=()):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if SERVER:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "no-referrer")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, where, headers=()):
        self.send_response(303)
        self.send_header("Location", where)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def authed(self):
        if not PASSWORD:
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "linotype" and hmac.compare_digest(v, COOKIE):
                return True
        return False

    def json(self, obj, code=200):
        self.send(json.dumps(obj), "application/json", code)

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_UPLOAD:
            raise ValueError("file too large")
        return self.rfile.read(n)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/login":
            return self.send(login_page())
        if path == "/source.tar.gz":
            return self.send(SOURCE, "application/gzip",
                             headers=[("Content-Disposition", 'attachment; filename="linotype-source.tar.gz"')])
        if not self.authed():
            return self.redirect("/login")
        if path == "/fonts/devanagari.ttf" and SERVER and os.path.exists(SHAPED_FONT):
            data = Path(SHAPED_FONT).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "font/ttf")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            return self.wfile.write(data)
        if path == "/":
            return self.send(home_page())
        m = re.fullmatch(r"/d/([\w-]+)(/info|/fonts|/p/(\d+))?", path)
        s = sessions.get(m.group(1)) if m else None
        if not s:
            if m and m.group(2) is None:  # expired document: back to the drop page
                return self.redirect("/")
            return self.send("Not found", "text/plain", 404)
        s.last = time.time()
        if m.group(2) is None:
            return self.send(SHELL)
        if m.group(2) == "/fonts":
            with s.lock:
                rows = font_report(s.doc, font_overrides(s))
            store = font_store(s)
            for r in rows:
                r["file"] = store[r["base"]][4] if r["base"] in store else None
            return self.json({"fonts": rows})
        if m.group(2) == "/info":
            return self.json({"name": s.name, "pages": [[p.rect.width, p.rect.height] for p in s.doc],
                              "restore": s.restore, "hasOriginal": s.has_original})
        n = int(m.group(3))
        if not 0 <= n < len(s.doc):
            return self.send("No such page", "text/plain", 404)
        return self.send(s.page_html(n))

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/login":
            form = parse_qs(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)).decode())
            if PASSWORD and hmac.compare_digest(form.get("password", [""])[0], PASSWORD):
                # Secure when the proxy says the visitor came over HTTPS (always, in production)
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                return self.redirect("/", [("Set-Cookie", f"linotype={COOKIE}; Path=/; Max-Age=2592000; "
                                                          f"HttpOnly; SameSite=Lax{secure}")])
            time.sleep(1)  # slow down guessing
            return self.send(login_page("That password isn't right."), code=401)
        if not self.authed():
            return self.json({"error": "Please sign in again."}, 401)
        try:
            if path == "/open":
                data = self.body()
                if not data.startswith(b"%PDF"):
                    return self.json({"error": "That doesn't look like a PDF."}, 400)
                name = unquote(self.headers.get("X-Filename", "document.pdf"))
                try:
                    return self.json({"id": new_session(Path(name).name, data)})
                except ValueError as e:
                    return self.json({"error": str(e)}, 400)
            m = re.fullmatch(r"/d/([\w-]+)/(save|close|reveal|font|font-remove)", path)
            s = sessions.get(m.group(1)) if m else None
            if not s:
                return self.json({"error": "This document was closed after being idle. Open the file again."}, 404)
            s.last = time.time()
            action = m.group(2)
            if action == "close":  # free the document and its rendered pages
                sessions.pop(m.group(1), None)
                return self.json({"ok": True})
            if action in ("font", "font-remove"):
                q = parse_qs(urlparse(self.path).query)
                name = q.get("name", [""])[0]
                warnings = []
                if action == "font":
                    filename = Path(q.get("file", ["font"])[0]).name
                    try:
                        kind, data, cov, warnings = load_user_font(self.body(), filename, name)
                    except ValueError as e:
                        return self.json({"error": str(e)}, 400)
                    font_store(s)[name] = (kind, data, cov, secrets.token_hex(4), filename)
                else:
                    font_store(s).pop(name, None)
                # re-render with the new font set: every open PDF locally, just this one on a server
                for other in ([s] if SERVER else list(sessions.values())):
                    with other.lock:
                        other.pages.clear()
                return self.json({"ok": True, "warnings": warnings})
            if action == "reveal":
                if s.saved:
                    reveal_file(str(s.saved))
                return self.json({"ok": bool(s.saved)})
            body = json.loads(self.body())
            if isinstance(body, list):  # older pages sent just the edit list
                body = {"edits": body, "state": [], "keep": False}
            edits = body["edits"]
            with s.lock:
                out = writeback.apply(s.data, edits, s.fonts)
                if body.get("keep"):
                    out = attach_original(out, s.data, body.get("state", []))
            if SERVER:  # hand the file to the browser; nothing is written on the server
                name = s.out_path(download=True)
                return self.send(out, "application/pdf", headers=[
                    ("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}"),
                    ("X-Linotype-Name", quote(name)), ("X-Linotype-Kept", "1" if body.get("keep") else "0")])
            dest = s.out_path()
            dest.write_bytes(out)
            s.saved = dest
            open_file(str(dest))
            return self.json({"path": str(dest), "name": dest.name, "kept": bool(body.get("keep"))})
        except Exception as e:  # show the reason in the page rather than a dead request
            return self.json({"error": f"{type(e).__name__}: {e}"}, 500)


HOME = r"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Linotype</title>
<style>
/* Base Web (baseweb.design) light/dark theme tokens */
:root{
  --bg:#FFFFFF;--bg2:#F3F3F3;--bg3:#E8E8E8;--inverse:#000000;--on-inverse:#FFFFFF;
  --fg:#000000;--fg2:#4B4B4B;--fg3:#5E5E5E;--border:rgba(0,0,0,.08);--border-strong:#DDDDDD;
  --accent:#276EF1;--accent-light:#EFF4FE;--positive:#0E8345;--positive-light:#EAF6ED;
  --warning:#9F6402;--warning-light:#FDF2DC;--negative:#DE1135;
  --font:system-ui,"Helvetica Neue",Helvetica,Arial,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#161616;--bg2:#292929;--bg3:#383838;--inverse:#DEDEDE;--on-inverse:#000000;
  --fg:#DEDEDE;--fg2:#C4C4C4;--fg3:#ABABAB;--border:rgba(222,222,222,.08);--border-strong:#383838;
  --accent:#5E8BDB;--accent-light:#1B2742;--positive:#5C9D70;--positive-light:#132A1B;
  --warning:#AE8523;--warning-light:#2E230C;--negative:#DE5B5D;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:400 16px/24px var(--font)}
main{max-width:760px;margin:0 auto;padding:48px 24px 64px}
h1{font:700 28px/36px var(--font);margin:0}
.lede{font:400 18px/28px var(--font);color:var(--fg2);margin:4px 0 32px}

/* FileUploader */
#drop{display:flex;flex-direction:column;align-items:center;gap:16px;padding:48px 24px;border-radius:12px;
  background:var(--bg2);border:2px dashed var(--border-strong);text-align:center;cursor:pointer;transition:background .15s,border-color .15s}
#drop:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
body.over #drop{border-color:var(--accent);background:var(--accent-light)}
#drop .title{font:500 18px/24px var(--font)}
#drop .hint{font:400 14px/20px var(--font);color:var(--fg3)}
.btn{font:500 16px/20px var(--font);border:0;border-radius:8px;padding:14px 20px;cursor:pointer}
.btn.primary{background:var(--inverse);color:var(--on-inverse)}
.btn.primary:hover{opacity:.86}
#msg{font:500 14px/20px var(--font);min-height:20px;color:var(--fg2)}
#msg.err{color:var(--negative)}

/* Tabs */
.tabs{display:flex;gap:24px;border-bottom:1px solid var(--border-strong);margin:40px 0 8px;overflow-x:auto}
.tabs button{font:500 16px/20px var(--font);background:none;border:0;border-bottom:2px solid transparent;
  padding:12px 0 14px;color:var(--fg3);cursor:pointer;white-space:nowrap}
.tabs button:hover{color:var(--fg)}
.tabs button[aria-selected=true]{color:var(--fg);border-bottom-color:var(--fg)}
.tabs button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
[role=tabpanel]{padding-top:8px}
[role=tabpanel][hidden]{display:none}

/* ListItem */
.list{list-style:none;margin:0;padding:0}
.item{display:flex;gap:16px;align-items:flex-start;padding:16px 0;border-bottom:1px solid var(--border)}
.item:last-child{border-bottom:0}
.art{flex:none;width:32px;height:32px;border-radius:50%;display:grid;place-items:center;background:var(--bg2);
  font:500 14px/16px var(--font);color:var(--fg)}
.label{font:500 16px/20px var(--font);margin-bottom:4px}
.desc{font:400 14px/20px var(--font);color:var(--fg2)}
.desc strong{color:var(--fg);font-weight:500}

/* Notification */
.note{display:flex;gap:12px;border-radius:8px;padding:12px 16px;margin:16px 0 4px;font:400 14px/20px var(--font)}
.note.info{background:var(--accent-light)}
.note.warn{background:var(--warning-light)}
.note b{font-weight:500}

.intro{font:400 16px/24px var(--font);color:var(--fg2);margin:12px 0 4px}
kbd{font:500 12px/16px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--bg2);border:1px solid var(--border-strong);
  border-radius:4px;padding:2px 6px;color:var(--fg)}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--border);font:400 12px/20px var(--font);color:var(--fg3)}
@media (max-width:560px){main{padding:32px 16px 48px}.tabs{gap:16px}}
</style>
<main>
  <h1>Linotype</h1>
  <p class="lede">Edit the text of any PDF, in its own fonts.</p>

  <div id="drop" tabindex="0" role="button" aria-label="Choose a PDF to edit">
    <div class="title">Drop a PDF here</div>
    <button class="btn primary" type="button" tabindex="-1">Choose a PDF…</button>
    <div class="hint">Or right-click a PDF in Finder → Open With → Linotype</div>
    <div id="msg" aria-live="polite"></div>
    <input id="pick" type="file" accept="application/pdf,.pdf" hidden>
  </div>

  <div class="tabs" role="tablist" aria-label="About Linotype">
    <button role="tab" id="t-how" aria-controls="p-how" aria-selected="true">How it works</button>
    <button role="tab" id="t-fonts" aria-controls="p-fonts" aria-selected="false" tabindex="-1">Fonts</button>
    <button role="tab" id="t-limits" aria-controls="p-limits" aria-selected="false" tabindex="-1">Limitations</button>
    <button role="tab" id="t-privacy" aria-controls="p-privacy" aria-selected="false" tabindex="-1">Privacy &amp; shortcuts</button>
  </div>

  <section role="tabpanel" id="p-how" aria-labelledby="t-how">
    <ul class="list">
      <li class="item"><span class="art">1</span><div><div class="label">Click any text and type</div>
        <div class="desc">The block turns into a text box in the PDF's own font and rewraps as you type. A block that grows past its space is outlined in red.</div></div></li>
      <li class="item"><span class="art">2</span><div><div class="label">Add or remove text</div>
        <div class="desc"><strong>+ Text</strong> then click the page to add a line. Hover a block and press <strong>✕</strong> to remove it; <strong>↩</strong> brings it back.</div></div></li>
      <li class="item"><span class="art">3</span><div><div class="label">Save a copy</div>
        <div class="desc"><strong>Save PDF</strong> writes <em>name</em>-edited.pdf: next to the original when opened from Finder, in Downloads when dropped here. The original is never changed.</div></div></li>
    </ul>
  </section>

  <section role="tabpanel" id="p-fonts" aria-labelledby="t-fonts" hidden>
    <p class="intro">Edits use the PDF's own fonts, so new words match the old ones. Most PDFs carry only a trimmed copy of each font, holding just the letters the document used.</p>
    <div class="note info"><span>ⓘ</span><span><b>For an exact match</b>, open <b>Aa Fonts</b> in the editor and add the original font file (.ttf, .otf, .ttc, .woff). Only the letters you use go into the saved PDF.</span></div>
    <ul class="list">
      <li class="item"><span class="art">Aa</span><div><div class="label">Missing letters</div>
        <div class="desc">A letter that isn't in the PDF's copy is drawn in a similar system font until you add the original.</div></div></li>
      <li class="item"><span class="art">§</span><div><div class="label">Licenses</div>
        <div class="desc">Fonts whose license forbids embedding are refused; "view &amp; print only" fonts get a warning. Only add fonts you're allowed to use.</div></div></li>
      <li class="item"><span class="art">⌫</span><div><div class="label">Nothing is kept</div>
        <div class="desc">Added fonts stay in memory while Linotype is open and are never written to disk.</div></div></li>
    </ul>
  </section>

  <section role="tabpanel" id="p-limits" aria-labelledby="t-limits" hidden>
    <div class="note warn"><span>⚠</span><span><b>Tested with</b> documents from Word, Google Docs, LaTeX, InDesign and Chrome, and with Hindi. Anything else may work but hasn't been checked yet.</span></div>
    <ul class="list">
      <li class="item"><span class="art">⎙</span><div><div class="label">Scans and pictures of text</div>
        <div class="desc">No text to edit. Canva exports draw letters as shapes, same problem.</div></div></li>
      <li class="item"><span class="art">⚿</span><div><div class="label">Password-protected PDFs</div>
        <div class="desc">Can't be opened. Remove the password first.</div></div></li>
      <li class="item"><span class="art">⊘</span><div><div class="label">Watermarks, stamps and legal notices</div>
        <div class="desc">Rotated, see-through or "all rights reserved" text is locked.</div></div></li>
      <li class="item"><span class="art">↧</span><div><div class="label">Text doesn't flow between blocks</div>
        <div class="desc">A paragraph that grows won't push the next one down.</div></div></li>
      <li class="item"><span class="art">✎</span><div><div class="label">Digital signatures</div>
        <div class="desc">Stop being valid once a signed PDF is edited.</div></div></li>
      <li class="item"><span class="art">…</span><div><div class="label">Not supported yet</div>
        <div class="desc">Right-to-left text (Arabic, Hebrew) is untested; form fields, comments and rotated pages aren't editable.</div></div></li>
    </ul>
  </section>

  <section role="tabpanel" id="p-privacy" aria-labelledby="t-privacy" hidden>
    <ul class="list">
      <li class="item"><span class="art">⌂</span><div><div class="label">Stays on this computer</div>
        <div class="desc">Linotype makes no network requests. Your PDFs and fonts are never uploaded anywhere.</div></div></li>
      <li class="item"><span class="art">↺</span><div><div class="label">Keep original</div>
        <div class="desc">On by default: the saved file carries the untouched original, so Linotype can reopen your edits or <strong>Revert to original</strong>. Anyone with the file can extract that original, so turn it off before sharing a file you edited to hide something.</div></div></li>
      <li class="item"><span class="art">T</span><div><div class="label">Real text, untouched rest</div>
        <div class="desc">Edited text is real, searchable text, not a picture pasted on top. Everything you didn't edit stays exactly as it was.</div></div></li>
      <li class="item"><span class="art">⌘</span><div><div class="label">Shortcuts</div>
        <div class="desc"><kbd>⌘ S</kbd> save · <kbd>⌘ O</kbd> open another · <kbd>⌘ +</kbd> <kbd>⌘ −</kbd> <kbd>⌘ 0</kbd> zoom · <kbd>Esc</kbd> stop editing</div></div></li>
    </ul>
  </section>

  <footer>Linotype is free, open-source software (AGPL-3.0).</footer>
</main>
<script>
const drop = document.getElementById('drop'), msg = document.getElementById('msg'), pick = document.getElementById('pick');
function say(text, err) { msg.textContent = text; msg.classList.toggle('err', !!err); }
async function openPdf(file) {
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') return say(`${file.name} isn't a PDF.`, true);
  say('Opening ' + file.name + '…');
  try {
    const r = await fetch('/open', {method: 'POST', headers: {'X-Filename': encodeURIComponent(file.name)}, body: file});
    const j = await r.json();
    if (j.id) location.href = '/d/' + j.id; else say(j.error, true);
  } catch (e) { say('Could not open the file: ' + e, true); }
}
drop.onclick = () => pick.click();
drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick.click(); } };
pick.onchange = () => openPdf(pick.files[0]);
// the whole window is a drop target, not just the box
let depth = 0;
addEventListener('dragenter', e => { e.preventDefault(); depth++; document.body.classList.add('over'); });
addEventListener('dragover', e => e.preventDefault());
addEventListener('dragleave', () => { if (--depth <= 0) { depth = 0; document.body.classList.remove('over'); } });
addEventListener('drop', e => { e.preventDefault(); depth = 0; document.body.classList.remove('over'); openPdf(e.dataTransfer.files[0]); });

// tabs: click, or arrow keys between them
const tabs = [...document.querySelectorAll('[role=tab]')];
function select(tab) {
  tabs.forEach(t => {
    const on = t === tab;
    t.setAttribute('aria-selected', on); t.tabIndex = on ? 0 : -1;
    document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
  });
}
tabs.forEach((t, i) => {
  t.onclick = () => select(t);
  t.onkeydown = e => {
    const d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
    if (d) { const n = tabs[(i + d + tabs.length) % tabs.length]; select(n); n.focus(); }
  };
});
</script>"""


SHELL = r"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Linotype</title>
<style>
:root{--bg:#F3F3F3;--fg:#000000;--muted:#5E5E5E;--bar:#FFFFFF;--line:#E8E8E8;--accent:#276EF1;--btn:#F3F3F3;--btn-h:#E8E8E8;
  --warn:#9F6402;--inverse:#000000;--on-inverse:#FFFFFF}
@media (prefers-color-scheme:dark){:root{--bg:#0F0F0F;--fg:#DEDEDE;--muted:#ABABAB;--bar:#161616;--line:#292929;--accent:#5E8BDB;
  --btn:#292929;--btn-h:#383838;--warn:#AE8523;--inverse:#DEDEDE;--on-inverse:#000000}}
body{margin:0;background:var(--bg);color:var(--fg);font:400 14px/20px system-ui,"Helvetica Neue",Helvetica,Arial,sans-serif}
header{position:sticky;top:0;z-index:5;display:flex;gap:8px;align-items:center;padding:8px 12px;background:var(--bar);border-bottom:1px solid var(--line);flex-wrap:wrap}
.grp{display:flex;gap:4px;align-items:center}
.sep{width:1px;height:22px;background:var(--line);margin:0 4px}
#name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:32ch}
#count{color:var(--muted);white-space:nowrap}
#count.dirty{color:var(--warn)}
.spacer{flex:1}
button{font-family:inherit;font-weight:500;font-size:14px;line-height:16px;border:0;border-radius:8px;padding:8px 12px;background:var(--btn);color:var(--fg);cursor:pointer;white-space:nowrap}
button:hover{background:var(--btn-h)}
button:disabled{opacity:.45;cursor:default}
#keepwrap{display:flex;gap:6px;align-items:center;font-weight:500;color:var(--muted);white-space:nowrap;cursor:pointer;padding:0 4px}
#keepwrap input{accent-color:var(--inverse);width:16px;height:16px;margin:0}
button[aria-pressed=true]{background:var(--inverse);color:var(--on-inverse)}
button.primary{background:var(--inverse);color:var(--on-inverse)}
#zoomv{min-width:4.5ch;text-align:center;color:var(--muted);font-variant-numeric:tabular-nums}
main{display:flex;flex-direction:column;align-items:center;gap:20px;padding:24px 16px 80px}
.pg{background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.15),0 8px 24px rgba(0,0,0,.08);overflow:hidden;flex:none}
.pg iframe{border:0;display:block;transform-origin:0 0}
#pageno{position:fixed;right:16px;bottom:16px;background:var(--bar);border:1px solid var(--line);border-radius:8px;padding:4px 10px;color:var(--muted);font-variant-numeric:tabular-nums}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);display:flex;gap:10px;align-items:center;background:#111;color:#fff;padding:10px 12px 10px 16px;border-radius:10px;opacity:0;pointer-events:none;transition:.2s;max-width:calc(100% - 32px)}
#toast.on{opacity:1;pointer-events:auto}
#toast button{background:#333;color:#fff}
#fonts{position:fixed;inset:0;z-index:19;display:none;place-items:center;background:rgba(0,0,0,.35);padding:16px}
#fonts.on{display:grid}
#fonts .card{background:var(--bar);color:var(--fg);border-radius:12px;padding:18px 18px 10px;width:min(640px,100%);
  max-height:min(80vh,720px);display:flex;flex-direction:column;box-shadow:0 12px 40px rgba(0,0,0,.3)}
.fhead{display:flex;align-items:center;justify-content:space-between}
.fhead h2{font-size:17px;margin:0}
.fnote{color:var(--muted);margin:8px 0 12px}
#fontlist{overflow:auto;margin:0 -18px;padding:0 18px}
.frow{display:flex;gap:12px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}
.frow.drop{background:color-mix(in srgb,var(--accent) 12%,transparent)}
.fmain{flex:1;min-width:0}
.fname{font-weight:600;overflow-wrap:anywhere}
.fmeta,.fsample{color:var(--muted);font-size:12.5px}
.fsample{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fstat{font-size:12.5px;margin-top:2px}
.fstat.ok{color:#16a34a}.fstat.warn{color:var(--warn)}
.fwarn{font-size:12.5px;color:var(--warn);margin-top:4px}
.frow .acts{display:flex;gap:6px;flex:none}
#ask{position:fixed;inset:0;z-index:20;display:none;place-items:center;background:rgba(0,0,0,.35);padding:16px}
#ask.on{display:grid}
#ask .card{background:var(--bar);color:var(--fg);border-radius:12px;padding:18px;width:min(380px,100%);box-shadow:0 12px 40px rgba(0,0,0,.3)}
#ask p{margin:0 0 16px}
#ask .row{display:flex;gap:8px;justify-content:flex-end}
#ask .danger{background:#dc2626;color:#fff;font-weight:600}
@media (max-width:720px){#name{max-width:18ch}.hide-narrow{display:none}}
</style>
<header>
  <div class="grp"><button id="back" title="Close this PDF and open another (⌘O)">← Open another</button></div>
  <span class="sep"></span>
  <span id="name"></span><span id="count">Click any text to edit</span>
  <span class="spacer"></span>
  <div class="grp hide-narrow" title="Zoom (⌘− ⌘+ ⌘0)">
    <button id="zout" aria-label="Zoom out">−</button><span id="zoomv">100%</span><button id="zin" aria-label="Zoom in">+</button>
    <button id="zfit">Fit</button>
  </div>
  <span class="sep hide-narrow"></span>
  <div class="grp">
    <button id="blocks" aria-pressed="false" title="Outline every block you can edit">▢ Blocks</button>
    <button id="fontsbtn" title="See this PDF's fonts and add the original font files">Aa Fonts</button>
    <button id="addtext" aria-pressed="false" title="Add a text box: click here, then click on the page (Esc cancels)">+ Text</button>
    <button id="discard" disabled title="Throw away all edits in this PDF">Discard</button>
    <button id="revert" hidden title="Throw away the edits saved in this file and show the original">Revert to original</button>
    <label id="keepwrap" title="Put the original PDF inside the saved file so Linotype can reopen your edits or revert them. Anyone with the file can extract the original: turn this off before sharing a file you edited to hide something."><input type="checkbox" id="keep" checked> Keep original</label>
    <button id="save" class="primary" title="Save an edited copy (⌘S)">Save PDF</button>
  </div>
</header>
<main id="pages"></main>
<div id="pageno"></div>
<div id="toast"><span id="toastmsg"></span><button id="toastbtn" hidden></button></div>
<div id="fonts" role="dialog" aria-modal="true" aria-labelledby="fontstitle"><div class="card">
  <div class="fhead"><h2 id="fontstitle">Fonts in this PDF</h2><button id="fontsclose" aria-label="Close">✕</button></div>
  <p class="fnote">PDFs usually carry only a trimmed copy of each font. Add the original font file to see,
  edit and save text in its real typeface. Font files stay on this computer and are forgotten when Linotype quits.</p>
  <div id="fontlist">Reading fonts…</div>
</div></div>
<div id="ask" role="dialog" aria-modal="true"><div class="card"><p id="askmsg"></p>
  <div class="row"><button id="askno">Cancel</button><button id="askyes" class="danger"></button></div></div></div>
<script>
const id = location.pathname.split('/')[2];
const $ = s => document.getElementById(s);
const frames = [], boxes = [], sizes = [], counts = [];
let zoom = 1, fitMode = true, dirty = false, showBlocks = false;
// the desktop window asks Python whether it may close; keep Python told, never make it ask us
function markDirty(v) {
  dirty = v;
  try { window.pywebview && window.pywebview.api && window.pywebview.api.set_dirty(v); } catch (e) {}
}
// called by desktop.py when the window is closing with unsaved edits
window.confirmClose = async () => {
  if (await ask('Close Linotype without saving your edits?', 'Close without saving')) {
    markDirty(false); window.pywebview.api.quit();
  }
};

function toast(text, action, ms = 5000) {
  $('toastmsg').textContent = text;
  const b = $('toastbtn');
  b.hidden = !action;
  if (action) { b.textContent = action.label; b.onclick = action.run; }
  $('toast').classList.add('on');
  clearTimeout(toast.t); toast.t = setTimeout(() => $('toast').classList.remove('on'), ms);
}

// The macOS app's WKWebView never shows window.confirm dialogs (it answers "cancel" at once),
// so confirmations are drawn in the page
function ask(text, yes) {
  $('askmsg').textContent = text; $('askyes').textContent = yes;
  $('ask').classList.add('on'); $('askyes').focus();
  return new Promise(done => {
    const finish = v => { $('ask').classList.remove('on'); removeEventListener('keydown', key, true); done(v); };
    const key = e => { if (e.key === 'Escape') { e.stopPropagation(); finish(false); } };
    addEventListener('keydown', key, true);
    $('askyes').onclick = () => finish(true);
    $('askno').onclick = () => finish(false);
    $('ask').onclick = e => { if (e.target === $('ask')) finish(false); };
  });
}

// ---- pages ----
// ---- a file Linotype saved with "Keep original": restore its edits as live boxes ----
let restore = [], restored = {}, restoring = {};
function restorePage(i) {
  const items = (restore.find(p => p.page === i) || {}).items;
  if (!items || !items.length || restored[i]) return;
  restored[i] = true;
  restoring[i] = frames[i].contentWindow.restoreState(items);
}
// pages not scrolled to yet still carry saved edits: load them before collecting a save
async function loadRestoredPages() {
  for (const p of restore) {
    const f = frames[p.page];
    if (!f) continue;
    if (!restored[p.page]) {
      f.loading = 'eager';
      await new Promise(ok => {
        const ready = () => f.contentWindow && f.contentWindow.restoreState && f.contentDocument.readyState === 'complete';
        if (ready()) ok(); else f.addEventListener('load', ok, {once: true});
      });
      restorePage(p.page);
    }
    await restoring[p.page];
  }
}

fetch(`/d/${id}/info`).then(r => r.ok ? r.json() : Promise.reject()).then(info => {
  $('name').textContent = info.name; $('name').title = info.name; document.title = info.name + ' · Linotype';
  restore = info.restore || [];
  $('revert').hidden = !info.hasOriginal;
  info.pages.forEach(([w, h], i) => {
    const box = document.createElement('div'); box.className = 'pg';
    const f = document.createElement('iframe');
    f.width = Math.ceil(w * 4 / 3); f.height = Math.ceil(h * 4 / 3);
    f.loading = i < 2 ? 'eager' : 'lazy';
    f.src = `/d/${id}/p/${i}?embed=1`;
    f.onload = () => { applyBlocks(f); restorePage(i); };
    box.appendChild(f); $('pages').appendChild(box);
    frames.push(f); boxes.push(box); sizes.push([+f.width, +f.height]); counts.push(0);
  });
  applyZoom(); watchPages();
}).catch(() => { $('name').textContent = 'This file is no longer open'; });

// ---- zoom: scale the iframes from outside, so glyph positions inside stay in PDF units ----
function fitZoom() {
  const avail = document.documentElement.clientWidth - 32;
  return Math.min(1.5, Math.max(0.25, avail / Math.max(...sizes.map(s => s[0]), 1)));
}
function applyZoom() {
  if (fitMode) zoom = fitZoom();
  frames.forEach((f, i) => {
    const [w, h] = sizes[i];
    f.style.transform = `scale(${zoom})`;
    boxes[i].style.width = w * zoom + 'px'; boxes[i].style.height = h * zoom + 'px';
  });
  $('zoomv').textContent = Math.round(zoom * 100) + '%';
  $('zfit').setAttribute('aria-pressed', fitMode);
}
function setZoom(z) { fitMode = false; zoom = Math.min(3, Math.max(0.25, Math.round(z * 20) / 20)); applyZoom(); }
$('zin').onclick = () => setZoom(zoom + 0.1);
$('zout').onclick = () => setZoom(zoom - 0.1);
$('zfit').onclick = () => { fitMode = true; applyZoom(); };
addEventListener('resize', () => fitMode && applyZoom());

// ---- page number ----
function watchPages() {
  const seen = new Map();
  const io = new IntersectionObserver(es => {
    es.forEach(e => seen.set(e.target, e.intersectionRatio));
    let best = 0, bi = 0;
    boxes.forEach((b, i) => { const r = seen.get(b) || 0; if (r > best) { best = r; bi = i; } });
    $('pageno').textContent = `Page ${bi + 1} / ${boxes.length}`;
  }, {threshold: [0, .25, .5, .75, 1]});
  boxes.forEach(b => io.observe(b));
  $('pageno').textContent = `Page 1 / ${boxes.length}`;
}

// ---- edit count ----
addEventListener('message', e => {
  const i = frames.findIndex(f => f.contentWindow === e.source);
  if (i < 0 || e.data?.type !== 'edits') return;
  counts[i] = e.data.n;
  const n = counts.reduce((a, b) => a + b, 0);
  if (n && !dirty && !e.data.restored) markDirty(true);
  $('count').textContent = n ? `${n} block${n > 1 ? 's' : ''} edited${dirty ? '' : ' · saved'}`
                              : dirty ? 'Reverted to original · not saved' : 'Click any text to edit';
  $('count').classList.toggle('dirty', n > 0 && dirty);
  $('discard').disabled = n === 0;
});

// ---- blocks outline ----
function applyBlocks(f) { try { f.contentDocument.body.classList.toggle('show', showBlocks); } catch (e) {} }
$('blocks').onclick = () => {
  showBlocks = !showBlocks; $('blocks').setAttribute('aria-pressed', showBlocks); frames.forEach(applyBlocks);
};

// ---- add text: the next click on any page places a new box ----
let adding = false;
function setAdding(on) {
  adding = on; $('addtext').setAttribute('aria-pressed', on);
  frames.forEach(f => { try { f.contentWindow.setAddMode && f.contentWindow.setAddMode(on); } catch (e) {} });
}
$('addtext').onclick = () => setAdding(!adding);
addEventListener('message', e => { if (e.data?.type === 'addDone') setAdding(false); });
addEventListener('keydown', e => { if (e.key === 'Escape' && adding) setAdding(false); });

// ---- fonts dialog ----
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const pagesText = ps => ps.length === 1 ? `page ${ps[0]}` :
  (ps.every((p, i) => !i || p === ps[i - 1] + 1) ? `pages ${ps[0]}–${ps.at(-1)}` : `${ps.length} pages`);
function fontStatus(f) {
  if (f.status === 'user') return ['ok', `✓ Using your file ${f.file}`];
  if (f.status === 'embedded')
    return f.missing ? ['warn', `Embedded · ${f.missing} character${f.missing > 1 ? 's' : ''} shown in a look-alike`]
                     : ['ok', '✓ Embedded in the PDF'];
  if (f.status === 'fallback')
    return ['warn', f.shaped ? "⚠ The PDF's copy can't join these letters — showing a system look-alike"
                             : "⚠ The PDF's copy is too incomplete — showing a system look-alike"];
  return ['warn', '⚠ Not included in the PDF — showing a system look-alike'];
}
async function loadFonts(notes = {}) {
  const list = $('fontlist');
  const {fonts} = await (await fetch(`/d/${id}/fonts`)).json();
  if (!fonts.length) { list.textContent = 'This PDF has no text.'; return; }
  list.innerHTML = fonts.map(f => {
    const [cls, msg] = fontStatus(f);
    return `<div class="frow" data-name="${esc(f.base)}">
      <div class="fmain">
        <div class="fname">${esc(f.base)}</div>
        <div class="fmeta">${f.chars.toLocaleString()} characters · ${pagesText(f.pages)}</div>
        <div class="fsample" lang="und">${esc(f.sample)}</div>
        <div class="fstat ${cls}">${esc(msg)}</div>
        ${notes[f.base] ? `<div class="fwarn">${esc(notes[f.base])}</div>` : ''}
      </div>
      <div class="acts">
        ${f.file ? '<button data-act="remove">Remove</button>' : ''}
        <button data-act="add" class="${f.status === 'embedded' && !f.missing ? '' : 'primary'}">${f.file ? 'Replace…' : 'Add font file…'}</button>
      </div></div>`;
  }).join('');
}
const picker = Object.assign(document.createElement('input'), {type: 'file', accept: '.ttf,.otf,.ttc,.woff,.woff2', hidden: true});
document.body.appendChild(picker);
let pickFor = null;
async function reloadPagesAfterFontChange() {
  frames.forEach((f, i) => { counts[i] = 0; f.src = f.src; });
  markDirty(false); $('count').textContent = 'Click any text to edit'; $('count').classList.remove('dirty');
  $('discard').disabled = true;
}
async function addFont(name, file) {
  if (dirty && !await ask('Adding a font reloads the pages, so unsaved edits are lost. Continue?', 'Add font')) return;
  const row = $('fontlist').querySelector(`.frow[data-name="${CSS.escape(name)}"] .fstat`);
  if (row) { row.className = 'fstat'; row.textContent = `Adding ${file.name}…`; }
  const r = await fetch(`/d/${id}/font?name=${encodeURIComponent(name)}&file=${encodeURIComponent(file.name)}`,
                        {method: 'POST', body: file});
  const j = await r.json();
  if (j.error) { await loadFonts({[name]: j.error}); return; }
  await reloadPagesAfterFontChange();
  await loadFonts(j.warnings && j.warnings.length ? {[name]: j.warnings.join(' ')} : {});
  toast(`Using ${file.name} for ${name}`);
}
picker.onchange = () => { if (picker.files[0] && pickFor) addFont(pickFor, picker.files[0]); picker.value = ''; };
$('fontlist').addEventListener('click', async e => {
  const b = e.target.closest('button'); if (!b) return;
  const name = b.closest('.frow').dataset.name;
  if (b.dataset.act === 'add') { pickFor = name; picker.click(); }
  if (b.dataset.act === 'remove') {
    if (dirty && !await ask('Removing a font reloads the pages, so unsaved edits are lost. Continue?', 'Remove font')) return;
    await fetch(`/d/${id}/font-remove?name=${encodeURIComponent(name)}`, {method: 'POST'});
    await reloadPagesAfterFontChange(); await loadFonts();
  }
});
// drop a font file straight onto its row
$('fontlist').addEventListener('dragover', e => {
  const row = e.target.closest('.frow'); if (!row) return;
  e.preventDefault(); row.classList.add('drop');
});
$('fontlist').addEventListener('dragleave', e => { const row = e.target.closest('.frow'); if (row) row.classList.remove('drop'); });
$('fontlist').addEventListener('drop', e => {
  const row = e.target.closest('.frow'); if (!row) return;
  e.preventDefault(); row.classList.remove('drop');
  if (e.dataTransfer.files[0]) addFont(row.dataset.name, e.dataTransfer.files[0]);
});
function openFonts() { $('fonts').classList.add('on'); $('fontlist').textContent = 'Reading fonts…'; loadFonts(); }
function closeFonts() { $('fonts').classList.remove('on'); }
$('fontsbtn').onclick = openFonts;
$('fontsclose').onclick = closeFonts;
$('fonts').addEventListener('click', e => { if (e.target === $('fonts')) closeFonts(); });
addEventListener('keydown', e => { if (e.key === 'Escape' && $('fonts').classList.contains('on') && !$('ask').classList.contains('on')) closeFonts(); });

// ---- discard ----
$('discard').onclick = async () => {
  if (!await ask('Throw away all edits in this PDF?', 'Discard edits')) return;
  frames.forEach((f, i) => { if (counts[i]) { counts[i] = 0; f.src = f.src; } });
  markDirty(false); $('count').textContent = 'Click any text to edit'; $('count').classList.remove('dirty');
  $('discard').disabled = true;
};

// ---- revert: drop the edits saved in this file, show the original it carries ----
$('revert').onclick = async () => {
  if (!await ask('Show the original PDF, without the edits saved in this file? Save afterwards to write it out.', 'Revert to original')) return;
  restore = []; restored = {}; restoring = {};
  frames.forEach((f, i) => { counts[i] = 0; f.src = f.src; });
  $('revert').hidden = true;
  markDirty(true);
  $('count').textContent = 'Reverted to original · not saved'; $('count').classList.add('dirty');
};

// ---- close / open another ----
async function closeDoc() {
  if (dirty && !await ask('You have unsaved edits. Close this PDF anyway?', 'Close without saving')) return;
  markDirty(false);
  await fetch(`/d/${id}/close`, {method: 'POST'}).catch(() => {});
  location.href = '/';
}
$('back').onclick = closeDoc;
addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });

// ---- save ----
async function save() {
  await loadRestoredPages();
  const edits = [], state = [];
  frames.forEach((f, page) => {
    const w = f.contentWindow;
    if (w && w.collectEdits) {
      const blocks = w.collectEdits(); if (blocks.length) edits.push({page, blocks});
      const items = w.collectState(); if (items.length) state.push({page, items});
    }
  });
  if (!edits.length) return toast('Nothing edited yet. Click any text on the page to change it.');
  const btn = $('save'); btn.disabled = true; btn.textContent = 'Saving…';
  try {
    const keep = $('keep').checked;
    const r = await fetch(`/d/${id}/save`, {method: 'POST', body: JSON.stringify({edits, state, keep})});
    if ((r.headers.get('Content-Type') || '').startsWith('application/pdf')) {
      // server mode: the saved PDF comes back as a download
      const name = decodeURIComponent(r.headers.get('X-Linotype-Name') || 'edited.pdf');
      const a = document.createElement('a');
      a.href = URL.createObjectURL(await r.blob()); a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
      markDirty(false); $('count').classList.remove('dirty');
      toast('Downloaded ' + name + (r.headers.get('X-Linotype-Kept') === '1' ? ' · original kept inside' : ''), null, 8000);
      btn.disabled = false; btn.textContent = 'Save PDF';
      return;
    }
    const j = await r.json();
    if (j.path) {
      markDirty(false); $('count').classList.remove('dirty');
      $('count').textContent = $('count').textContent.replace(/ · saved$/, '') + ' · saved';
      toast('Saved ' + j.name + (j.kept ? ' · original kept inside' : ''),
            {label: 'Show in Finder', run: () => fetch(`/d/${id}/reveal`, {method: 'POST'})}, 8000);
    } else toast(j.error, null, 8000);
  } catch (e) { toast('Save failed: ' + e); }
  btn.disabled = false; btn.textContent = 'Save PDF';
}
$('save').onclick = save;

addEventListener('keydown', e => {
  if (!(e.metaKey || e.ctrlKey)) return;
  const k = e.key;
  if (k === 's') { e.preventDefault(); save(); }
  else if (k === 'o') { e.preventDefault(); closeDoc(); }
  else if (k === '=' || k === '+') { e.preventDefault(); setZoom(zoom + 0.1); }
  else if (k === '-') { e.preventDefault(); setZoom(zoom - 0.1); }
  else if (k === '0') { e.preventDefault(); fitMode = true; applyZoom(); }
});

// #selftest: edit the first two blocks on page 1 and save (used for end-to-end checks)
if (location.hash === '#selftest') setTimeout(function run() {
  const w = frames[0] && frames[0].contentWindow, d = w && w.document;
  const eds = d && [...d.querySelectorAll('.ed')].filter(e => e.textContent.trim().length > 20);
  if (!eds || !eds.length || !d.fonts || d.fonts.status !== 'loaded') return setTimeout(run, 300);
  eds.slice(0, 2).forEach((ed, k) => {
    ed.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
    const p = ed.querySelector('p');
    p.textContent = k ? p.textContent + ' This sentence was added by the self-test to check reflow.' : 'Edited headline';
    ed.dispatchEvent(new Event('input')); ed.blur();
  });
  save().then(() => document.title = 'SELFTEST ' + $('toastmsg').textContent);
}, 500);
</script>"""


# the drop page says different things when it runs on a server
SERVER_TEXT = [
    ('<div class="hint">Or right-click a PDF in Finder → Open With → Linotype</div>',
     '<div class="hint">PDFs up to {mb} MB. Your file is kept in memory on this server only while you edit it.</div>'),
    ('<strong>Save PDF</strong> writes <em>name</em>-edited.pdf: next to the original when opened from Finder, in Downloads when dropped here. The original is never changed.',
     '<strong>Save PDF</strong> downloads <em>name</em>-edited.pdf. Your original file isn\'t changed.'),
    ('Added fonts stay in memory while Linotype is open and are never written to disk.',
     'Added fonts are used only for your document, kept in memory, and deleted with it.'),
    ('<div class="label">Stays on this computer</div>\n        <div class="desc">Linotype makes no network requests. Your PDFs and fonts are never uploaded anywhere.</div>',
     '<div class="label">Processed on this server, never stored</div>\n        <div class="desc">Your PDF is uploaded here only to show and save it. It stays in memory, is never written to disk, and is deleted when you close it or after {idle} minutes idle.</div>'),
    ('<footer>Linotype is free, open-source software (AGPL-3.0).</footer>',
     '<footer>Linotype is free, open-source software (AGPL-3.0). <a href="/source.tar.gz" style="color:inherit">Download the source code</a>.</footer>'),
]


def home_page():
    if not SERVER:
        return HOME
    page = HOME
    for old, new in SERVER_TEXT:
        page = page.replace(old, new.format(mb=MAX_MB, idle=IDLE // 60))
    return page


def login_page(error=""):
    note = f'<p class="err">{error}</p>' if error else ""
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Linotype · Sign in</title>
<style>
:root{{--bg:#FFFFFF;--bg2:#F3F3F3;--fg:#000000;--fg2:#4B4B4B;--line:#DDDDDD;--inverse:#000000;--on-inverse:#FFFFFF;--neg:#DE1135}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161616;--bg2:#292929;--fg:#DEDEDE;--fg2:#C4C4C4;--line:#383838;--inverse:#DEDEDE;--on-inverse:#000000;--neg:#DE5B5D}}}}
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
  font:400 16px/24px system-ui,"Helvetica Neue",Helvetica,Arial,sans-serif;padding:16px}}
form{{width:min(360px,100%)}}
h1{{font:700 28px/36px inherit;font-family:inherit;margin:0 0 4px}}
p{{margin:0 0 24px;color:var(--fg2)}}
label{{display:block;font-weight:500;font-size:14px;margin-bottom:8px}}
input{{width:100%;font:inherit;padding:12px 14px;border-radius:8px;border:2px solid var(--bg2);background:var(--bg2);color:var(--fg);outline:none}}
input:focus{{border-color:var(--fg)}}
button{{width:100%;margin-top:16px;font:inherit;font-weight:500;border:0;border-radius:8px;padding:14px;background:var(--inverse);color:var(--on-inverse);cursor:pointer}}
.err{{color:var(--neg);margin:12px 0 0;font-size:14px}}
</style>
<form method="post" action="/login">
  <h1>Linotype</h1><p>Edit the text of any PDF, in its own fonts.</p>
  <label for="pw">Password</label>
  <input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
  {note}
  <button type="submit">Sign in</button>
</form>"""


SOURCE = source_tarball()


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 8091 if SERVER else 0))
    if SERVER and not PASSWORD:
        sys.exit("Server mode needs LINOTYPE_PASSWORD set.")
    server = ThreadingHTTPServer((host, port), Handler)
    if SERVER:
        threading.Thread(target=expire_sessions, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    if len(sys.argv) > 1:
        src = Path(sys.argv[1]).expanduser().resolve()
        url += "d/" + new_session(src.name, src.read_bytes(), src)
    print(f"Linotype running at {url}  (Ctrl+C to stop)", flush=True)
    if not os.environ.get("NO_BROWSER") and not SERVER:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
