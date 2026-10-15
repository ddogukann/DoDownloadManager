import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import APP_NAME, VERSION
from .video import ytdlp_binary

EXTENSION_SCHEMES = ("moz-extension://", "chrome-extension://")
MAX_BODY = 2 * 1024 * 1024


def valid_url(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Bridge:
    def __init__(self, manager, settings, token, on_download):
        self.manager = manager
        self.settings = settings
        self.token = token
        self.on_download = on_download
        self.port = int(settings.get("bridge_port") or 47821)
        self.server = None
        self.thread = None
        self.error = None

    def start(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f"DDM/{VERSION}"

            def log_message(self, *args):
                pass

            def _origin_ok(self):
                origin = self.headers.get("Origin")
                if origin and not origin.startswith(EXTENSION_SCHEMES):
                    return False
                host = (self.headers.get("Host") or "").lower()
                if host not in (f"127.0.0.1:{bridge.port}", f"localhost:{bridge.port}"):
                    return False
                return True

            def _authorized(self):
                if not self._origin_ok():
                    return False
                tok = self.headers.get("X-DDM-Token")
                if tok:
                    return hmac.compare_digest(tok, bridge.token)
                return bool(self.headers.get("X-DDM-Client"))

            def _cors(self):
                origin = self.headers.get("Origin")
                if origin and origin.startswith(EXTENSION_SCHEMES):
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")

            def _send(self, code, payload):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_BODY:
                    return None
                try:
                    return json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return None

            def do_OPTIONS(self):
                origin = self.headers.get("Origin") or ""
                if not origin.startswith(EXTENSION_SCHEMES) or not self._origin_ok():
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(204)
                self._cors()
                self.send_header("Access-Control-Allow-Methods", "GET, POST")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-DDM-Client, X-DDM-Token")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if not self._authorized():
                    return self._send(403, {"ok": False, "error": "forbidden"})
                path = urlsplit(self.path).path
                if path == "/ping":
                    return self._send(200, bridge.ping())
                if path == "/downloads":
                    return self._send(200, {"ok": True, "downloads": bridge.list_downloads()})
                return self._send(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                if not self._authorized():
                    return self._send(403, {"ok": False, "error": "forbidden"})
                path = urlsplit(self.path).path
                data = self._body()
                if not isinstance(data, dict):
                    return self._send(400, {"ok": False, "error": "invalid request"})
                if path == "/download":
                    code, payload = bridge.handle_download(data)
                    return self._send(code, payload)
                if path == "/action":
                    code, payload = bridge.handle_action(data)
                    return self._send(code, payload)
                return self._send(404, {"ok": False, "error": "not found"})

        try:
            self.server = _Server(("127.0.0.1", self.port), Handler)
        except OSError as e:
            self.error = str(e)
            return False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="bridge")
        self.thread.start()
        return True

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def ping(self):
        return {
            "ok": True, "app": APP_NAME, "version": VERSION,
            "ytdlp": bool(ytdlp_binary(self.settings)),
            "active": self.manager.active_count(),
            "config": {
                "capture_exts": self.settings.get("capture_exts"),
                "capture_min_kib": int(self.settings.get("capture_min_kib") or 0),
            },
        }

    def list_downloads(self):
        progress = self.manager.progress()
        out = []
        for r in sorted(self.manager.records(), key=lambda r: r.get("created") or 0, reverse=True)[:50]:
            snap = progress.get(r["id"]) or {}
            out.append({
                "id": r["id"], "url": r["url"], "kind": r["kind"],
                "filename": r.get("filename") or "", "status": r["status"], "error": r.get("error"),
                "total": snap.get("total", r.get("total")), "downloaded": snap.get("downloaded", r.get("downloaded") or 0),
                "speed": snap.get("speed", 0),
                "vt": {"verdict": (r.get("vt") or {}).get("verdict"),
                       "malicious": ((r.get("vt") or {}).get("stats") or {}).get("malicious"),
                       "total": ((r.get("vt") or {}).get("stats") or {}).get("total"),
                       "mismatch": bool((r.get("vt") or {}).get("mismatch"))},
            })
        return out

    def handle_download(self, data):
        url = (data.get("url") or "").strip()
        if not valid_url(url):
            return 400, {"ok": False, "error": "invalid URL"}
        kind = data.get("kind") if data.get("kind") in ("file", "video") else "file"
        headers = {}
        if data.get("referer") and valid_url(data["referer"]):
            headers["Referer"] = data["referer"]
        if data.get("userAgent"):
            headers["User-Agent"] = str(data["userAgent"])[:512]
        if data.get("cookieHeader") and kind == "file":
            headers["Cookie"] = str(data["cookieHeader"])[:16384]
        cookies = data.get("cookies") if isinstance(data.get("cookies"), list) else None
        request = {
            "url": url, "kind": kind, "headers": headers,
            "filename": (data.get("filename") or "").strip() or None,
            "title": (data.get("title") or "").strip()[:300] or None,
            "page_url": data.get("pageUrl") if valid_url(data.get("pageUrl") or "") else None,
            "cookies": cookies[:500] if cookies else None,
            "size": data.get("size") if isinstance(data.get("size"), int) else None,
            "source": "browser",
        }
        result = self.on_download(request)
        return 200, {"ok": True, **(result or {})}

    def handle_action(self, data):
        did = data.get("id")
        action = data.get("action")
        if not self.manager.get(did):
            return 404, {"ok": False, "error": "not found"}
        if action == "pause":
            self.manager.pause(did)
        elif action == "resume":
            self.manager.resume(did)
        elif action in ("cancel", "remove"):
            self.manager.remove(did)
        else:
            return 400, {"ok": False, "error": "unknown action"}
        return 200, {"ok": True}
