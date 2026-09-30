"""Linotype desktop app: the engine's pages in a native window.

    uv run desktop.py              # window with the drop page
    uv run desktop.py file.pdf     # window straight into the editor

pywebview draws the window with the system web view (WKWebView on macOS, WebView2 on
Windows), the same one a browser tab of `app.py` would get on Safari/Edge. The engine's
server runs in a thread of this process, still on 127.0.0.1 only.
"""
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import webview

import app


def _hook_open_files(open_path):
    """macOS hands files to an app (Finder double-click, "Open With", a drop on the Dock
    icon) as an openURLs event on the application delegate, both at launch and while the
    app is running. pywebview's delegate doesn't handle it, so extend it before the window
    is created."""
    from webview.platforms.cocoa import BrowserView

    class LinotypeAppDelegate(BrowserView.AppDelegate):
        def application_openURLs_(self, application, urls):
            for url in urls:
                if url.isFileURL():
                    open_path(url.path())

    BrowserView.AppDelegate = LinotypeAppDelegate


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/"

    def editor_url(path):
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            return None
        data = path.read_bytes()
        if not data.startswith(b"%PDF"):
            return None
        try:
            return base + "d/" + app.new_session(path.name, data, path)
        except ValueError:  # e.g. password-protected; the drop page explains when opened there
            return None

    state = {"dirty": False}

    class Api:  # called from the page as window.pywebview.api.*
        def set_dirty(self, value):
            state["dirty"] = bool(value)

        def quit(self):
            state["dirty"] = False
            if sys.platform == "darwin":
                # window.destroy() ends in NSApp.stop_, which only takes effect on the next
                # input event; terminate goes through the normal quit path, now allowed
                from AppKit import NSApp
                from PyObjCTools import AppHelper
                AppHelper.callAfter(NSApp.terminate_, None)
            else:
                window.destroy()

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    start = (editor_url(args[-1]) if args else None) or base
    window = webview.create_window("Linotype", start, width=1100, height=900, min_size=(480, 400),
                                   js_api=Api())

    def open_path(path):
        url = editor_url(path)
        if url:
            window.load_url(url)

    def closing():
        # Runs on the main thread while the window closes. It must not wait on the page:
        # evaluate_js needs the main thread too, so waiting here deadlocks the app. The page
        # keeps state["dirty"] current instead; with unsaved edits, cancel the close and let
        # the page ask (its answer calls Api.quit).
        if not state["dirty"]:
            return True
        threading.Thread(target=window.evaluate_js, args=("confirmClose()",), daemon=True).start()
        return False

    window.events.closing += closing
    if sys.platform == "darwin":
        _hook_open_files(open_path)
    webview.start()
    server.shutdown()


if __name__ == "__main__":
    main()
