"""Always-on static server for the preview port (8095).

Serves canvas/_site: a folder `<name>/` there is `<public url>/<name>/`. The
agent publishes by writing or symlinking into it, the operator by copying a
folder in on the host. It used to run only when the agent started an
http.server itself, so the public URL answered 502 after every restart.

No directory listings (the root would list every published folder) and no
dotfiles; HTML is sent with no-cache so a republished page shows on reload.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("python-sandbox.preview")

SITE_DIR = os.environ.get("PYTHON_SANDBOX_SITE_DIR", "/home/node/.openclaw/canvas/_site")
PORT = int(os.environ.get("PYTHON_SANDBOX_SITE_PORT", "8095"))

mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/wasm", ".wasm")


class SiteHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        if any(part.startswith(".") for part in self.path.split("?", 1)[0].split("/") if part):
            self.send_error(404)
            return None
        return super().send_head()

    def list_directory(self, path):
        self.send_error(404)
        return None

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        log.debug("%s %s", self.address_string(), format % args)


def start() -> None:
    os.makedirs(SITE_DIR, exist_ok=True)
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), partial(SiteHandler, directory=SITE_DIR))
    except OSError as e:
        # The agent's own dev server holds the port; it keeps it until it exits.
        log.warning("preview server not started on :%d: %s", PORT, e)
        return
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="preview-server", daemon=True).start()
    log.info("preview server on :%d serving %s", PORT, SITE_DIR)
