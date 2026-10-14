#!/usr/bin/env python3
"""
Mock of the IISER (Sophos/Cyberoam) captive portal, for testing the app on an emulator.

  python3 tools/mock_portal.py [--user test] [--password secret] [--port 8090]

Endpoints mimic the real one:
  GET  /generate_204   204 when signed in, 302 to the portal page otherwise
  GET  /httpclient.html the portal page (200)
  POST /login.xml       mode=191&username=..&password=..   -> <status>LIVE|LOGIN</status>
  POST /logout.xml      mode=193&username=..               -> signs out
Admin (for tests):
  GET  /admin/expire    simulate the gateway session timing out
  GET  /admin/state     JSON: logged_in, login_attempts, ...
  GET  /admin/limit/1   make every login answer "maximum login limit reached"
"""
import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE = {"logged_in": False, "login_attempts": 0, "logins_ok": 0, "limit": False}
CFG = {}


def xml(status, message):
    return (
        '<?xml version="1.0" encoding="UTF-8"?><requestresponse>'
        f"<status><![CDATA[{status}]]></status><message><![CDATA[{message}]]></message>"
        "</requestresponse>"
    ).encode()


class H(BaseHTTPRequestHandler):
    server_version = "xxxx"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body=b"", ctype="text/xml", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/generate_204":
            if STATE["logged_in"]:
                return self._send(204)
            return self._send(302, b"", headers={"Location": f"http://{self.headers.get('Host')}/httpclient.html"})
        if path == "/httpclient.html":
            return self._send(200, b"<html><body>Sophos portal</body></html>", "text/html")
        if path == "/admin/expire":
            STATE["logged_in"] = False
            return self._send(200, b"expired", "text/plain")
        if path == "/admin/state":
            return self._send(200, json.dumps(STATE).encode(), "application/json")
        if path.startswith("/admin/limit/"):
            STATE["limit"] = path.endswith("/1")
            return self._send(200, b"ok", "text/plain")
        self._send(404, b"nope", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(n).decode())
        path = urlparse(self.path).path
        user = form.get("username", [""])[0]
        sys.stderr.write(
            "      %s Origin=%s Referer=%s CT=%s fields=%s\n"
            % (path, self.headers.get("Origin"), self.headers.get("Referer"),
               self.headers.get("Content-Type"), sorted(form))
        )
        if path == "/login.xml":
            STATE["login_attempts"] += 1
            if STATE["limit"]:
                return self._send(200, xml("LOGIN", "You have reached the maximum login limit."))
            ok = user == CFG["user"] and form.get("password", [""])[0] == CFG["password"]
            if ok:
                STATE["logged_in"] = True
                STATE["logins_ok"] += 1
                return self._send(200, xml("LIVE", "You have successfully logged in"))
            return self._send(200, xml("LOGIN", "The system could not log you on. Make sure your password is correct"))
        if path == "/logout.xml":
            STATE["logged_in"] = False
            return self._send(200, xml("LOGIN", "You have successfully logged off"))
        self._send(404, b"nope", "text/plain")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="test")
    ap.add_argument("--password", default="secret")
    ap.add_argument("--port", type=int, default=8090)
    a = ap.parse_args()
    CFG.update(user=a.user, password=a.password)
    print(f"mock portal on :{a.port}  user={a.user!r}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()
