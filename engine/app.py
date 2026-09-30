"""Linotype engine: drop a PDF, edit it in the browser, save back to PDF.

    uv run app.py              # opens a drop page
    uv run app.py file.pdf     # opens straight into the editor; saves next to the file

Everything runs on 127.0.0.1; the PDF never leaves the machine. The desktop app runs this as a
sidecar and reads the `LINOTYPE_URL` / `LINOTYPE_TOKEN` lines it prints at startup.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pymupdf

import writeback
from render import render_page

MAX_UPLOAD = 300 * 1024 * 1024
TOKEN = secrets.token_urlsafe(16)  # guards /open-path, which reads files by path
sessions = {}


class Session:
    def __init__(self, name, data, src=None):
        self.name, self.data, self.src = name, data, src
        self.doc = pymupdf.open("pdf", data)
        self.lock = threading.Lock()  # a MuPDF document isn't safe to use from two threads
        self.pages = {}
        self.fonts = {}

    def page_html(self, n):
        with self.lock:
            if n not in self.pages:
                self.pages[n], _ = render_page(self.doc, n, editable=True, font_sink=self.fonts)
            return self.pages[n]

    def out_path(self):
        stem = Path(self.name).stem
        folder = Path(self.src).parent if self.src else Path.home() / "Downloads"
        path, i = folder / f"{stem}-edited.pdf", 2
        while path.exists():
            path, i = folder / f"{stem}-edited-{i}.pdf", i + 1
        return path


def new_session(name, data, src=None):
    sid = secrets.token_urlsafe(8)
    sessions[sid] = Session(name, data, src)
    return sid


def open_file(path):
    if os.environ.get("NO_OPEN"):
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606 - opening the user's own file with their viewer
    else:
        subprocess.Popen(["xdg-open", path])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="text/html; charset=utf-8", code=200):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(json.dumps(obj), "application/json", code)

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_UPLOAD:
            raise ValueError("file too large")
        return self.rfile.read(n)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self.send(HOME)
        if path == "/open-path":
            # the desktop shell opens files from Finder / "Open With" through here
            q = parse_qs(urlparse(self.path).query)
            src = Path(q.get("path", [""])[0])
            if q.get("token", [""])[0] != TOKEN or not src.is_file():
                return self.send("Not allowed", "text/plain", 403)
            data = src.read_bytes()
            if not data.startswith(b"%PDF"):
                return self.send("That doesn't look like a PDF.", "text/plain", 400)
            self.send_response(302)
            self.send_header("Location", "/d/" + new_session(src.name, data, src))
            self.end_headers()
            return
        m = re.fullmatch(r"/d/([\w-]+)(/info|/p/(\d+))?", path)
        s = sessions.get(m.group(1)) if m else None
        if not s:
            return self.send("Not found", "text/plain", 404)
        if m.group(2) is None:
            return self.send(SHELL)
        if m.group(2) == "/info":
            return self.json({"name": s.name, "pages": [[p.rect.width, p.rect.height] for p in s.doc]})
        n = int(m.group(3))
        if not 0 <= n < len(s.doc):
            return self.send("No such page", "text/plain", 404)
        return self.send(s.page_html(n))

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/open":
                data = self.body()
                if not data.startswith(b"%PDF"):
                    return self.json({"error": "That doesn't look like a PDF."}, 400)
                name = unquote(self.headers.get("X-Filename", "document.pdf"))
                return self.json({"id": new_session(Path(name).name, data)})
            m = re.fullmatch(r"/d/([\w-]+)/save", path)
            s = sessions.get(m.group(1)) if m else None
            if not s:
                return self.json({"error": "Session expired. Open the file again."}, 404)
            edits = json.loads(self.body())
            with s.lock:
                out = writeback.apply(s.data, edits, s.fonts)
            dest = s.out_path()
            dest.write_bytes(out)
            open_file(str(dest))
            return self.json({"path": str(dest)})
        except Exception as e:  # show the reason in the page rather than a dead request
            return self.json({"error": f"{type(e).__name__}: {e}"}, 500)


HOME = r"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Linotype</title>
<style>
:root{--bg:#f6f6f4;--fg:#1b1b1b;--muted:#6b6b6b;--line:#cfcfca;--accent:#2f5bea;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--muted:#9a9a9a;--line:#3a3a3a;--accent:#7c9cff;--card:#1f1f1f}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;padding:16px}
#drop{width:min(560px,100%);border:2px dashed var(--line);border-radius:16px;background:var(--card);padding:56px 24px;text-align:center;cursor:pointer;transition:.15s}
#drop.over{border-color:var(--accent);transform:scale(1.01)}
h1{font-size:22px;margin:0 0 6px}p{margin:0;color:var(--muted)}#msg{margin-top:14px;min-height:1.5em;color:var(--accent)}
</style>
<div id="drop"><h1>Drop a PDF here</h1><p>or click to choose one. It stays on this computer.</p><div id="msg"></div>
<input id="pick" type="file" accept="application/pdf,.pdf" hidden></div>
<script>
const drop = document.getElementById('drop'), msg = document.getElementById('msg'), pick = document.getElementById('pick');
async function openPdf(file) {
  if (!file) return;
  msg.textContent = 'Opening ' + file.name + '…';
  const r = await fetch('/open', {method: 'POST', headers: {'X-Filename': encodeURIComponent(file.name)}, body: file});
  const j = await r.json();
  if (j.id) location.href = '/d/' + j.id; else msg.textContent = j.error;
}
drop.onclick = () => pick.click();
pick.onchange = () => openPdf(pick.files[0]);
['dragenter', 'dragover'].forEach(t => addEventListener(t, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(t => addEventListener(t, e => { e.preventDefault(); drop.classList.remove('over'); }));
addEventListener('drop', e => openPdf(e.dataTransfer.files[0]));
</script>"""

SHELL = r"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Linotype</title>
<style>
:root{--bg:#e9e9e6;--fg:#1b1b1b;--muted:#6b6b6b;--bar:#fff;--line:#d6d6d1;--accent:#2f5bea}
@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#eee;--muted:#9a9a9a;--bar:#1c1c1c;--line:#333;--accent:#7c9cff}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;display:flex;gap:12px;align-items:center;padding:10px 16px;background:var(--bar);border-bottom:1px solid var(--line)}
#name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}#count{color:var(--muted);margin-left:auto;white-space:nowrap}
button{font:inherit;font-weight:600;border:0;border-radius:8px;padding:8px 14px;background:var(--accent);color:#fff;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
main{display:flex;flex-direction:column;align-items:center;gap:20px;padding:24px 16px 80px}
.pg{background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.15),0 8px 24px rgba(0,0,0,.08);max-width:100%;overflow:auto}
.pg iframe{border:0;display:block}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#111;color:#fff;padding:10px 16px;border-radius:10px;opacity:0;transition:.2s;max-width:calc(100% - 32px)}
#toast.on{opacity:1}
</style>
<header><span id="name"></span><span id="count">Click any text to edit</span><button id="save">Save PDF</button></header>
<main id="pages"></main><div id="toast"></div>
<script>
const id = location.pathname.split('/')[2];
const frames = [], counts = [];
const toast = (t, ms = 4000) => { const el = document.getElementById('toast'); el.textContent = t; el.classList.add('on');
  clearTimeout(toast.t); toast.t = setTimeout(() => el.classList.remove('on'), ms); };
fetch(`/d/${id}/info`).then(r => r.json()).then(info => {
  document.getElementById('name').textContent = info.name; document.title = info.name + ' · Linotype';
  info.pages.forEach(([w, h], i) => {
    const box = document.createElement('div'); box.className = 'pg';
    const f = document.createElement('iframe');
    f.width = Math.ceil(w * 4 / 3); f.height = Math.ceil(h * 4 / 3);
    f.loading = i < 2 ? 'eager' : 'lazy';
    f.src = `/d/${id}/p/${i}?embed=1`;
    box.appendChild(f); document.getElementById('pages').appendChild(box);
    frames.push(f); counts.push(0);
  });
});
addEventListener('message', e => {
  const i = frames.findIndex(f => f.contentWindow === e.source);
  if (i < 0 || e.data?.type !== 'edits') return;
  counts[i] = e.data.n;
  const n = counts.reduce((a, b) => a + b, 0);
  document.getElementById('count').textContent = n ? `${n} block${n > 1 ? 's' : ''} edited` : 'Click any text to edit';
});
async function save() {
  const edits = [];
  frames.forEach((f, page) => {
    const w = f.contentWindow;
    if (w && w.collectEdits) { const blocks = w.collectEdits(); if (blocks.length) edits.push({page, blocks}); }
  });
  if (!edits.length) return toast('Nothing edited yet. Click any text on the page to change it.');
  const btn = document.getElementById('save'); btn.disabled = true; btn.textContent = 'Saving…';
  try {
    const j = await (await fetch(`/d/${id}/save`, {method: 'POST', body: JSON.stringify(edits)})).json();
    toast(j.path ? 'Saved and opened ' + j.path : j.error, 6000);
  } catch (e) { toast('Save failed: ' + e); }
  btn.disabled = false; btn.textContent = 'Save PDF';
}
document.getElementById('save').onclick = save;
addEventListener('keydown', e => { if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); save(); } });
// #selftest: edit the first two blocks on page 1 and save (used by `app.py --selftest`)
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
  save().then(() => document.title = 'SELFTEST ' + document.getElementById('toast').textContent);
}, 500);
</script>"""


def main():
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", 0))), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    if len(sys.argv) > 1:
        src = Path(sys.argv[1]).expanduser().resolve()
        url += "d/" + new_session(src.name, src.read_bytes(), src)
    print(f"Linotype running at {url}  (Ctrl+C to stop)", flush=True)
    print(f"LINOTYPE_URL {url}", flush=True)
    print(f"LINOTYPE_TOKEN {TOKEN}", flush=True)
    if not os.environ.get("NO_BROWSER"):
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
