"""Catch the Schwab OAuth redirect locally instead of copying it by hand.

Schwab authorisation codes expire roughly thirty seconds after you approve the
consent screen. Selecting the redirected URL out of the address bar, switching
windows and pasting it into a terminal frequently loses that race, and the
failure is indistinguishable from a misconfiguration::

    invalid_grant: "Authorization code is invalid, expired or revoked"

Nothing is wrong in that case except the clock. So for the duration of the
login this module puts an HTTPS server on the callback address. The browser's
otherwise-doomed request to ``127.0.0.1:8182`` is answered rather than refused,
the code is read straight from the query string, and the timed window shrinks
to a browser round trip.

Design constraints, in the order they matter:

* **Never block the fallback.** Every failure here — port taken, no certificate
  tooling, headless machine, browser on a different host — is reported and
  degrades to the manual paste. Nothing in this module raises into the login
  flow.
* **Loopback only.** The socket binds the callback host, which the setup docs
  fix at ``127.0.0.1``. It is never reachable off the machine.
* **One code, then stop.** The server shuts down as soon as it has an answer.
  It exists for a few seconds a week.

The certificate is self-signed and kept in the state directory, so the browser
warns once per browser rather than once per login.
"""

from __future__ import annotations

import http.server
import logging
import os
import shutil
import socket
import ssl
import subprocess
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("trading.oauth")

# Long enough that a cert outlives the project; short enough to be unremarkable.
_CERT_DAYS = 3650

_SUCCESS_PAGE = b"""<!doctype html>
<meta charset="utf-8">
<title>Authorised</title>
<style>
  body { font: 16px/1.6 system-ui, sans-serif; background: #0f1115; color: #e6e6e6;
         display: grid; place-items: center; height: 100vh; margin: 0; }
  .card { text-align: center; padding: 2.5rem 3rem; background: #181b22;
          border: 1px solid #2a2f3a; border-radius: 12px; }
  h1 { font-size: 1.25rem; margin: 0 0 .5rem; color: #4ade80; }
  p { margin: 0; color: #9aa4b2; }
</style>
<div class="card">
  <h1>Authorisation received</h1>
  <p>Return to your terminal. You can close this tab.</p>
</div>
"""

_DENIED_PAGE = b"""<!doctype html>
<meta charset="utf-8">
<title>Not authorised</title>
<style>
  body { font: 16px/1.6 system-ui, sans-serif; background: #0f1115; color: #e6e6e6;
         display: grid; place-items: center; height: 100vh; margin: 0; }
  .card { text-align: center; padding: 2.5rem 3rem; background: #181b22;
          border: 1px solid #2a2f3a; border-radius: 12px; }
  h1 { font-size: 1.25rem; margin: 0 0 .5rem; color: #f87171; }
  p { margin: 0; color: #9aa4b2; }
</style>
<div class="card">
  <h1>Authorisation was not granted</h1>
  <p>Return to your terminal for the details.</p>
</div>
"""


class CertificateUnavailable(RuntimeError):
    """No way to produce a self-signed certificate on this machine."""


# --- certificate -------------------------------------------------------------


def _write_cert_with_cryptography(cert: Path, key: Path, host: str) -> None:
    import datetime as _dt
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])

    try:
        alt: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        alt = x509.DNSName(host)

    now = _dt.datetime.now(_dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=_CERT_DAYS))
        .add_extension(x509.SubjectAlternativeName([alt]), critical=False)
        .sign(private, hashes.SHA256())
    )

    key.write_bytes(private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def _write_cert_with_openssl(cert: Path, key: Path, host: str) -> None:
    openssl = shutil.which("openssl")
    if not openssl:
        raise CertificateUnavailable("openssl is not on PATH")

    # An IP in the SAN must be declared as IP:, not DNS:, or the browser
    # rejects it outright rather than merely warning about the signature.
    try:
        socket.inet_aton(host)
        san = f"IP:{host}"
    except OSError:
        san = f"DNS:{host}"

    result = subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key), "-out", str(cert),
            "-days", str(_CERT_DAYS), "-nodes",
            "-subj", f"/CN={host}",
            "-addext", f"subjectAltName={san}",
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise CertificateUnavailable(
            f"openssl failed: {result.stderr.strip()[:200]}"
        )


def ensure_certificate(state_dir: Path, host: str) -> tuple[Path, Path]:
    """Return ``(cert, key)`` for *host*, generating them once and reusing them.

    Reuse is the point: a certificate that changes every login would make the
    browser warn every login. Raises :class:`CertificateUnavailable` when the
    machine has neither ``cryptography`` nor ``openssl``.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    cert = state_dir / f"oauth_cert_{host.replace(':', '_')}.pem"
    key = state_dir / f"oauth_key_{host.replace(':', '_')}.pem"

    if cert.exists() and key.exists() and cert.stat().st_size and key.stat().st_size:
        return cert, key

    try:
        _write_cert_with_cryptography(cert, key, host)
    except ImportError:
        _write_cert_with_openssl(cert, key, host)
    except CertificateUnavailable:
        raise
    except Exception as exc:  # a broken cryptography install is not fatal
        log.debug("cryptography path failed (%s); trying openssl", exc)
        _write_cert_with_openssl(cert, key, host)

    os.chmod(key, 0o600)
    os.chmod(cert, 0o644)
    return cert, key


# --- server ------------------------------------------------------------------


@dataclass
class Redirect:
    """What the browser delivered: a code, or the reason there isn't one."""

    code: str | None = None
    error: str | None = None


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "qqq-oauth"
    #: Drop a connection that sends nothing. VS Code's port forwarder opens
    #: idle ones; without this each pins a worker thread forever.
    timeout = 15

    def do_GET(self) -> None:  # noqa: N802  (stdlib naming)
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        code = (params.get("code") or [""])[0]
        error = (params.get("error") or [""])[0]

        if not code and not error:
            # Chrome asks for /favicon.ico unprompted. Answering 404 without
            # resolving keeps the server waiting for the real redirect.
            self.send_error(404)
            return

        if error:
            description = (params.get("error_description") or [""])[0]
            self.server.redirect = Redirect(  # type: ignore[attr-defined]
                error=f"{error}: {description}" if description else error
            )
            body = _DENIED_PAGE
        else:
            self.server.redirect = Redirect(code=code)  # type: ignore[attr-defined]
            body = _SUCCESS_PAGE

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.received.set()  # type: ignore[attr-defined]

    def log_message(self, *_args) -> None:
        """Silence the stdlib access log; it would garble the CLI output."""


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    redirect: Redirect | None = None

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.received = threading.Event()

    def handle_error(self, *_args) -> None:
        """Swallow connection errors.

        Declining the certificate warning aborts the TLS handshake, which the
        stdlib would otherwise dump as a traceback into the middle of the
        login instructions.
        """


class RedirectCatcher:
    """A one-shot HTTPS server on the OAuth callback address.

    Use :func:`start_catcher`, which never raises, rather than constructing
    this directly.
    """

    def __init__(self, server: _Server, url: str) -> None:
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.url = url

    def start(self) -> None:
        self._thread.start()

    def wait(self, timeout: float) -> Redirect | None:
        """Block until the browser arrives, or *timeout* seconds pass."""
        if not self._server.received.wait(timeout):
            return None
        return self._server.redirect

    def cancel(self) -> None:
        """Release the waiter without a result — used when stdin wins."""
        self._server.received.set()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> RedirectCatcher:
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def start_catcher(
    callback_url: str, state_dir: Path
) -> tuple[RedirectCatcher | None, str | None]:
    """Try to listen on *callback_url*. Returns ``(catcher, reason_it_failed)``.

    Never raises. A ``None`` catcher means the caller should fall back to
    asking the user to paste the URL, and *reason* says why in one line.
    """
    parsed = urllib.parse.urlparse(callback_url)
    host = parsed.hostname
    if not host:
        return None, f"callback URL has no host: {callback_url!r}"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        server = _Server((host, port), _Handler)
    except OSError as exc:
        return None, f"cannot listen on {host}:{port} — {exc}"

    if parsed.scheme == "https":
        try:
            cert, key = ensure_certificate(state_dir, host)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            # Handshake lazily, in the per-connection worker thread. Done at
            # accept() it runs on the one serving thread, so a single client
            # that connects and stays silent (VS Code's port forwarder does
            # this) blocks every later connection, the real redirect included.
            server.socket = context.wrap_socket(
                server.socket, server_side=True, do_handshake_on_connect=False
            )
        except Exception as exc:
            server.server_close()
            return None, f"no TLS certificate available — {exc}"

    return RedirectCatcher(server, callback_url), None
