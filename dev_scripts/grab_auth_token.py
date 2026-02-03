from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        code = qs.get("code", [""])[0]
        print("\nAUTH CODE:", code or "<missing>")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"You can close this tab now.")
        if code:
            # stop server after first code
            raise SystemExit

HTTPServer(("", 8080), Handler).serve_forever()
