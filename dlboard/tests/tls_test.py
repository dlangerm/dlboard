"""
Native TLS (#797): a real `dlboard serve` with a certificate and key answers HTTPS (HTTP/2 when offered) and refuses
plain HTTP on its port, signs people in over it with no proxy in front, takes a client that trusts its certificate through
`DLBOARD_CA_BUNDLE`, serves a renewed certificate once its workers restart, and passes the image's health check.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import requests
from pydantic import SecretStr, ValidationError

from dlboard._wire import WHOAMI_PATH
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.conftest import DEADLINE_S, Key, free_port, make_certificate, needs_openssl, until
from dlboard.plugins.auth.password import create_or_reset_user
from dlboard.plugins.data_stores.sqlite import SQLLiteStore
from dlboard.serve._backend._api_tokens import mint_api_token
from dlboard.serve._production_server import TLSSettings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = needs_openssl

_DEPLOYMENT = """
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_STORAGE, PASSWORD_AUTH, themes

PLUGINS = [*LOCAL_STORAGE, *PASSWORD_AUTH, *BUILTIN_BACKEND, themes.default]
"""
"""
A password deployment: sign-in sets a `Secure` session cookie, which only comes back over HTTPS. A module of its own on
the server's path: the server imports the installed `dlboard`, which has no tests in it.
"""

_USER, _PASSWORD = "alice", "a long enough password"


class _Server:
    def __init__(self, tmp_path: Path) -> None:
        self.cert, self.key = make_certificate(tmp_path)
        self.port = free_port()
        self.url = f"https://127.0.0.1:{self.port}"
        db = tmp_path / "db.sqlite"
        self.store = SQLLiteStore(db)
        user = create_or_reset_user(self.store, _USER, _PASSWORD)
        self.token = SecretStr(mint_api_token(self.store, user, "tls test")[1])
        (tmp_path / "tls_deployment.py").write_text(_DEPLOYMENT)
        env = {
            **os.environ,
            "PYTHONPATH": str(tmp_path),
            "DLBOARD_SQLITE_LOCATION": str(db),
            "DLBOARD_ARTIFACT_STORE_LOCATION": str(tmp_path / "artifacts"),
            "DLBOARD_SECRET_KEY": "x" * 64,
            "DLBOARD_CUSTODIAN_ENABLED": "false",
            "DLBOARD_SSL_CERTIFICATE": str(self.cert),
            "DLBOARD_SSL_KEYFILE": str(self.key),
        }
        self.log = (tmp_path / "server.log").open("w")
        self.master = subprocess.Popen(
            [
                str(Path(sys.executable).with_name("dlboard")),
                *("serve", "custom", "--plugins", "tls_deployment:PLUGINS"),
                *("--port", str(self.port), "--workers", "1"),
            ],
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        try:
            self.wait_until_served(lambda fingerprint: fingerprint is not None)
        except BaseException:
            self.master.kill()
            self.stop()
            raise

    def fingerprint(self) -> str | None:
        """The SHA-256 of the certificate the port serves now, or `None` if it isn't serving yet."""
        context = ssl.create_default_context()
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE  # what it serves, trusted or not
        try:
            with (
                socket.create_connection(("127.0.0.1", self.port), timeout=2) as raw,
                context.wrap_socket(raw) as tls,
            ):
                der = tls.getpeercert(binary_form=True)
        except OSError:
            return None
        return hashlib.sha256(der).hexdigest() if der else None

    def wait_until_served(self, served: Callable[[str | None], bool]) -> None:
        def serving() -> bool | None:
            assert self.master.poll() is None, "the server exited"
            return served(self.fingerprint()) or None

        until("the server to serve its certificate", serving, timeout=3 * DEADLINE_S)

    def stop(self) -> None:
        self.master.terminate()
        self.master.wait(timeout=3 * DEADLINE_S)
        self.store.dispose()
        self.log.close()


@pytest.fixture
def server(tmp_path: Path) -> Iterator[_Server]:
    started = _Server(tmp_path)
    yield started
    started.stop()


def test_the_port_answers_https_and_refuses_plain_http(server: _Server) -> None:
    assert requests.get(f"{server.url}/healthz", verify=str(server.cert), timeout=10).ok
    with pytest.raises(requests.ConnectionError):
        requests.get(f"http://127.0.0.1:{server.port}/healthz", timeout=10)


def test_http2_is_offered_to_a_client_that_asks_for_it(server: _Server) -> None:
    context = ssl.create_default_context(cafile=server.cert)
    context.set_alpn_protocols(["h2", "http/1.1"])
    with (
        socket.create_connection(("127.0.0.1", server.port), timeout=10) as raw,
        context.wrap_socket(raw, server_hostname="127.0.0.1") as tls,
    ):
        assert tls.selected_alpn_protocol() == "h2"


def test_a_person_signs_in_over_tls_with_no_proxy_in_front(server: _Server) -> None:
    """The session cookie is `Secure`: it only comes back because the request really is HTTPS."""
    session, trust = requests.Session(), str(server.cert)
    form = session.get(f"{server.url}/login", verify=trust, timeout=10)
    match = re.search(r'name="csrf" value="([^"]+)"', form.text)
    assert match is not None
    signing_in = {"username": _USER, "password": _PASSWORD, "csrf": match[1]}
    session.post(f"{server.url}/login", data=signing_in, verify=trust, timeout=10)
    assert session.get(f"{server.url}/{WHOAMI_PATH}", verify=trust, timeout=10).json()["username"] == _USER


def test_a_client_trusts_the_certificate_through_its_ca_bundle(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_CA_BUNDLE", str(server.cert))
    monkeypatch.setenv(
        "REQUESTS_CA_BUNDLE", "/nonexistent"
    )  # what `requests` would let override a session's own
    assert BasicDlboardAPI(server.url, api_key=server.token).whoami().username == _USER


def test_a_renewed_certificate_is_served_once_the_workers_restart(server: _Server, tmp_path: Path) -> None:
    old = server.fingerprint()
    for made, served in zip(make_certificate(tmp_path / "renewed"), (server.cert, server.key), strict=True):
        made.replace(served)
    server.master.send_signal(signal.SIGHUP)
    server.wait_until_served(lambda fingerprint: fingerprint not in {None, old})


def test_the_image_health_check_asks_over_https_when_the_server_serves_it(server: _Server) -> None:
    """By `DLBOARD_SSL_CERTIFICATE`, which the container's environment has as the server's does; plain HTTP is refused."""
    plain = {k: v for k, v in os.environ.items() if k != "DLBOARD_SSL_CERTIFICATE"} | {
        "DLBOARD_PORT": str(server.port)
    }

    def healthy(env: dict[str, str]) -> bool:
        command = [sys.executable, "-m", "dlboard.scripts.healthcheck"]
        return subprocess.run(command, env=env, check=False, timeout=DEADLINE_S).returncode == 0

    assert healthy({**plain, "DLBOARD_SSL_CERTIFICATE": str(server.cert)})
    assert not healthy(plain)


class _Wrong(Enum):
    ANOTHER_CERTIFICATES = "another certificate's"
    ENCRYPTED = "encrypted"


@pytest.mark.parametrize("wrong", list(_Wrong), ids=[w.value for w in _Wrong])
def test_a_key_that_does_not_load_is_an_error_before_any_worker_starts(
    wrong: _Wrong, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Said once, by the server, rather than by each worker's panic; and never by OpenSSL asking for a password."""
    match wrong:
        case _Wrong.ANOTHER_CERTIFICATES:
            cert, _ = make_certificate(tmp_path / "pair")
            _, key = make_certificate(tmp_path / "other")
        case _Wrong.ENCRYPTED:
            cert, key = make_certificate(tmp_path / "pair", Key.ENCRYPTED)
    monkeypatch.setenv("DLBOARD_SSL_CERTIFICATE", str(cert))
    monkeypatch.setenv("DLBOARD_SSL_KEYFILE", str(key))
    with pytest.raises(ValidationError, match="don't load as a pair"):
        TLSSettings()
