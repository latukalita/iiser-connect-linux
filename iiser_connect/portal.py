"""Talks to the Sophos/Cyberoam captive portal exactly like its own web page does.

    POST https://<gateway>:8090/login.xml    mode=191&username=..&password=..&a=<ms>&producttype=0
    POST https://<gateway>:8090/logout.xml   mode=193&username=..&a=<ms>&producttype=0

Credentials are only ever sent over HTTPS, and the certificate must validate for the gateway's
host name - even when the gateway is reached through an IP address.
(Tests may point at a plain-HTTP mock on loopback via the ``base_override`` argument; it is
refused for any non-loopback host.)
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

PROBE_URL = "http://connectivitycheck.gstatic.com/generate_204"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
GATEWAY_DOMAIN = "iisertvm.ac.in"
REACH_TIMEOUT = 3.0           # "is the gateway here?" must answer fast, or it isn't

BAD_CREDENTIAL_HINTS = (
    "could not log you on", "password", "invalid", "incorrect", "credential", "authentication failed",
)
PERMANENT_HINTS = (
    "maximum", "limit", "exceeded", "expired", "disabled", "blocked", "locked", "not allowed", "deactivated",
)


@dataclass
class Accepted:
    message: str


@dataclass
class Rejected:
    message: str
    bad_credentials: bool
    permanent: bool


@dataclass
class Failure:
    error: str


Result = Accepted | Rejected | Failure


@dataclass
class Probe:
    online: bool
    redirect: str | None = None      # where a captive portal tried to send us (its Location header)


@dataclass
class Reply:
    status: int
    text: str
    location: str | None = None


def tag(xml: str, name: str) -> str | None:
    """Pull ``<name>value</name>``, tolerating CDATA and stray markup/whitespace."""
    m = re.search(rf"<{name}>(.*?)</{name}>", xml, re.S | re.I)
    if not m:
        return None
    t = m.group(1).replace("<![CDATA[", "").replace("]]>", "")
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"\s+", " ", t).strip()


def interpret(code: int, text: str, expect_live: bool) -> Result:
    status = tag(text, "status")
    message = tag(text, "message") or ""
    if not 200 <= code < 300 or status is None:
        return Failure(f"Unexpected reply from the gateway (HTTP {code})")
    if not expect_live:
        return Accepted(message or "Signed out")
    if status.upper() == "LIVE":
        return Accepted(message or "Signed in")
    m = message.lower()
    bad = any(h in m for h in BAD_CREDENTIAL_HINTS)
    permanent = bad or any(h in m for h in PERMANENT_HINTS)
    return Rejected(message or "The gateway refused the sign-in", bad, permanent)


class PortalClient:
    def __init__(self, host: str, ip_fallback: str = "", base_override: str | None = None, timeout: float = 6.0):
        self.host = host                 # certificate must be valid for this name
        self.ip = ip_fallback.strip()    # configured backup address
        self.seen_ip = ""                # address the portal's own redirect pointed us at (see adopt)
        self.timeout = timeout
        self.base = None
        if base_override:
            u = urlparse(base_override)
            if u.hostname not in LOOPBACK:
                raise ValueError("test base override must be a loopback address")
            self.base = u
        self._ctx = ssl.create_default_context()

    # -- finding the gateway ---------------------------------------------------------------
    def adopt(self, location: str | None) -> None:
        """Learn where the gateway is on THIS network from the portal's redirect.

        The redirect is only used as a hint for *where to connect*. Whatever it says, the TLS
        certificate must still be valid for an ``*.iisertvm.ac.in`` name, so a fake portal on some
        other network can never collect a password. Anything else in the redirect is ignored.
        """
        if self.base is not None or not location:
            return
        try:
            h = urlparse(location).hostname
        except ValueError:
            return
        if not h:
            return
        h = h.lower()
        if h == GATEWAY_DOMAIN or h.endswith("." + GATEWAY_DOMAIN):
            self.host = h
            return
        try:
            ip = ipaddress.ip_address(h)
        except ValueError:
            return
        if ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local:
            self.seen_ip = str(ip)

    def _routes(self):
        yield None                               # the gateway's own name, via DNS
        if self.base is None:
            for ip in dict.fromkeys(i for i in (self.seen_ip, self.ip) if i):
                yield ip

    # -- low level ------------------------------------------------------------------------
    def _open(self, via_ip: str | None, timeout: float) -> http.client.HTTPConnection:
        if self.base is not None:
            c = http.client.HTTPConnection(self.base.hostname, self.base.port or 80, timeout=timeout)
            c.connect()
            return c
        c = http.client.HTTPSConnection(self.host, 8090, timeout=timeout, context=self._ctx)
        if via_ip is None:
            c.connect()
            return c
        # Reach the gateway by IP, but verify the certificate for the real host name (SNI too).
        sock = socket.create_connection((via_ip, 8090), timeout=timeout)
        try:
            c.sock = self._ctx.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise
        return c

    def _request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
                 timeout: float | None = None) -> Reply:
        last: Exception | None = None
        for via_ip in self._routes():
            try:
                conn = self._open(via_ip, timeout or self.timeout)
            except OSError as e:          # couldn't connect: nothing was sent, so trying another route is safe
                last = e
                continue
            try:
                conn.request(method, path, body=body, headers=headers or {})
                r = conn.getresponse()
                return Reply(r.status, r.read(65536).decode("utf-8", "replace"), r.getheader("Location"))
            finally:
                conn.close()
        raise last or OSError("no route to the gateway")

    def _origin(self) -> str:
        return f"{self.base.scheme}://{self.base.netloc}" if self.base else f"https://{self.host}:8090"

    # -- probes ---------------------------------------------------------------------------
    def probe(self) -> Probe:
        """One tiny plain-HTTP request to Google's generate_204.

        204 means the internet is reachable. Anything else (a redirect to a login page, an error,
        a timeout) means it isn't, and a redirect also tells us where the portal lives.
        """
        try:
            if self.base is not None:
                r = self._request("GET", "/generate_204")
            else:
                u = urlparse(PROBE_URL)
                conn = http.client.HTTPConnection(u.hostname, 80, timeout=4)
                try:
                    conn.request("GET", u.path)
                    resp = conn.getresponse()
                    r = Reply(resp.status, "", resp.getheader("Location"))
                finally:
                    conn.close()
        except (OSError, http.client.HTTPException):
            return Probe(False)
        return Probe(True) if r.status == 204 else Probe(False, r.location)

    def has_internet(self) -> bool:
        return self.probe().online

    def portal_reachable(self) -> bool:
        try:
            return self._request("GET", "/httpclient.html", timeout=REACH_TIMEOUT).status == 200
        except (OSError, http.client.HTTPException):
            return False

    # -- actions --------------------------------------------------------------------------
    def login(self, username: str, password: str) -> Result:
        return self._post("/login.xml", {"mode": 191, "username": username, "password": password}, True)

    def logout(self, username: str) -> Result:
        return self._post("/logout.xml", {"mode": 193, "username": username}, False)

    def _post(self, path: str, fields: dict, expect_live: bool) -> Result:
        fields = dict(fields, a=int(time.time() * 1000), producttype=0)
        body = urlencode(fields).encode()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self._origin(),
            "Referer": self._origin() + "/httpclient.html",
        }
        try:
            r = self._request("POST", path, body, headers)
        except socket.timeout:
            return Failure("The gateway took too long to answer")
        except socket.gaierror:
            return Failure("Couldn't find the gateway on this network")
        except ssl.SSLError:
            return Failure("Couldn't verify the gateway's certificate")
        except (OSError, http.client.HTTPException) as e:
            return Failure((str(e) or "Network error")[:80])
        return interpret(r.status, r.text, expect_live)
