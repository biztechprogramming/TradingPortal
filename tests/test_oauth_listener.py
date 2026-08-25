"""The local OAuth redirect catcher.

The point of this component is that Schwab authorisation codes expire in about
thirty seconds, so the tests care most about the two things that would silently
push a user back over that limit: the trailing ``@`` (``%40``) surviving the
round trip, and an unrelated browser request such as ``/favicon.ico`` not
resolving the wait early.
"""

from __future__ import annotations

import socket
import ssl
import threading

import httpx
import pytest

from trading.oauth_listener import (
    RedirectCatcher,
    ensure_certificate,
    start_catcher,
)
from trading.cli import _code_from_url

# A real Schwab code: dotted, base64url, and terminated by an encoded '@'.
REAL_CODE = "C0.b2F1dGgyLmJkYy5zY2h3YWIuY29t.N9UpAETcBWbfflWr-NR0n-xaP8Dfhag@"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _fetch(url: str) -> httpx.Response:
    return httpx.get(url, verify=False, timeout=10)


# --- URL parsing -------------------------------------------------------------


def test_code_from_url_decodes_the_trailing_at():
    url = ("https://127.0.0.1:8182/?code=C0.abc.def%40"
           "&session=4e4215fe-c5d4-4607-9faa-3a03685b68a7")
    assert _code_from_url(url) == "C0.abc.def@"


def test_code_from_url_tolerates_whitespace_from_a_paste():
    assert _code_from_url("  https://127.0.0.1:8182/?code=xyz\n  ") == "xyz"


def test_code_from_url_returns_empty_when_absent():
    assert _code_from_url("https://127.0.0.1:8182/?error=access_denied") == ""


# --- certificate -------------------------------------------------------------


def test_certificate_is_generated_and_then_reused(tmp_path):
    cert, key = ensure_certificate(tmp_path, "127.0.0.1")
    assert cert.exists() and key.exists()
    assert key.stat().st_mode & 0o777 == 0o600

    # Reuse matters: a fresh certificate every login would warn every login.
    first = cert.read_bytes()
    again, _ = ensure_certificate(tmp_path, "127.0.0.1")
    assert again.read_bytes() == first


def test_certificate_loads_into_a_tls_context(tmp_path):
    cert, key = ensure_certificate(tmp_path, "127.0.0.1")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)  # raises if malformed


# --- capture -----------------------------------------------------------------


def test_catcher_captures_the_code_over_https(tmp_path):
    port = _free_port()
    catcher, why = start_catcher(f"https://127.0.0.1:{port}/", tmp_path)
    assert catcher is not None, why

    with catcher:
        result: list = []
        waiter = threading.Thread(target=lambda: result.append(catcher.wait(10)))
        waiter.start()

        encoded = REAL_CODE.replace("@", "%40")
        response = _fetch(f"https://127.0.0.1:{port}/?code={encoded}&session=abc")
        assert response.status_code == 200
        assert b"Authorisation received" in response.content

        waiter.join(10)

    assert result[0].code == REAL_CODE
    assert result[0].error is None


def test_favicon_does_not_end_the_wait(tmp_path):
    port = _free_port()
    catcher, why = start_catcher(f"https://127.0.0.1:{port}/", tmp_path)
    assert catcher is not None, why

    with catcher:
        response = _fetch(f"https://127.0.0.1:{port}/favicon.ico")
        assert response.status_code == 404
        # Still waiting: a stray browser request must not resolve the login.
        assert catcher.wait(0.5) is None


def test_denied_authorisation_is_reported_not_silently_awaited(tmp_path):
    port = _free_port()
    catcher, why = start_catcher(f"https://127.0.0.1:{port}/", tmp_path)
    assert catcher is not None, why

    with catcher:
        result: list = []
        waiter = threading.Thread(target=lambda: result.append(catcher.wait(10)))
        waiter.start()
        _fetch(f"https://127.0.0.1:{port}/"
               "?error=access_denied&error_description=User+declined")
        waiter.join(10)

    assert result[0].code is None
    assert "access_denied" in result[0].error
    assert "User declined" in result[0].error


def test_cancel_releases_the_waiter_without_a_code(tmp_path):
    """Used when a pasted URL beats the browser."""
    port = _free_port()
    catcher, _ = start_catcher(f"https://127.0.0.1:{port}/", tmp_path)
    with catcher:
        result: list = []
        waiter = threading.Thread(target=lambda: result.append(catcher.wait(10)))
        waiter.start()
        catcher.cancel()
        waiter.join(10)
    assert result[0] is None


# --- degradation -------------------------------------------------------------


def test_busy_port_degrades_instead_of_raising(tmp_path):
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]

        catcher, why = start_catcher(f"https://127.0.0.1:{port}/", tmp_path)

    assert catcher is None
    assert "cannot listen" in why


def test_callback_without_a_host_degrades(tmp_path):
    catcher, why = start_catcher("not-a-url", tmp_path)
    assert catcher is None
    assert "no host" in why


def test_plain_http_callback_needs_no_certificate(tmp_path):
    """Not the documented setup, but it must not demand TLS tooling."""
    port = _free_port()
    catcher, why = start_catcher(f"http://127.0.0.1:{port}/", tmp_path)
    assert catcher is not None, why
    with catcher:
        result: list = []
        waiter = threading.Thread(target=lambda: result.append(catcher.wait(10)))
        waiter.start()
        httpx.get(f"http://127.0.0.1:{port}/?code=plain", timeout=10)
        waiter.join(10)
    assert result[0].code == "plain"
    assert not list(tmp_path.glob("oauth_cert_*"))
