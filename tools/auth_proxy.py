import http.client
import secrets
import hmac
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 8765
ACCESS_TOKEN = secrets.token_urlsafe(32)
COOKIE_TOKEN = secrets.token_urlsafe(32)
ACCESS_PATH = "/access/" + ACCESS_TOKEN

class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _authorized(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        cookie = cookies.get("longflow_access")
        return bool(cookie and hmac.compare_digest(cookie.value.encode(), COOKIE_TOKEN.encode()))

    def _challenge(self):
        body = b"Authentication required\n"
        self.send_response(401)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self): self._proxy()
    def do_HEAD(self): self._proxy()
    def do_POST(self): self._proxy()
    def do_PUT(self): self._proxy()
    def do_PATCH(self): self._proxy()
    def do_DELETE(self): self._proxy()
    def do_OPTIONS(self): self._proxy()

    def _proxy(self):
        if self.command == "GET" and hmac.compare_digest(self.path.encode(), ACCESS_PATH.encode()):
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", "longflow_access=" + COOKIE_TOKEN + "; Path=/; Secure; HttpOnly; SameSite=Strict")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._authorized():
            self._challenge()
            return
        if self.headers.get("Transfer-Encoding"):
            self.send_error(400, "Transfer encoding unsupported")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "Invalid content length")
            return
        if length < 0 or length > 20_000_000:
            self.send_error(413, "Request too large")
            return
        body = self.rfile.read(length) if length else None
        headers = {}
        for key, value in self.headers.items():
            if key.lower() not in {"host", "connection", "content-length", "authorization", "cookie"}:
                headers[key] = value
        headers["Host"] = self.headers.get("Host", f"{UPSTREAM_HOST}:{UPSTREAM_PORT}")
        try:
            conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=300)
            conn.request(self.command, self.path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in {"connection", "transfer-encoding", "content-length"}:
                    self.send_header(key, value)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            conn.close()
        except Exception:
            self.send_error(502, "Upstream unavailable")

    def log_message(self, fmt, *args):
        message = (fmt % args).replace(ACCESS_PATH, "/access/[redacted]")
        print(f"{self.client_address[0]} - {message}", flush=True)

if __name__ == "__main__":
    print("ACCESS_PATH=" + ACCESS_PATH, flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8876), ProxyHandler).serve_forever()
