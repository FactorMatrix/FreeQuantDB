"""Explicit CLI bind addresses for the existing native BT provider."""

import json
import socket
import subprocess
import sys

import pytest

from fqdb.service.client import Client
from fqdb.service.seeding import normalize_listen
from fqdb.util import FQDBError
from test_contracts import setup_store


@pytest.mark.parametrize(
    ("address", "expected"),
    [("127.0.0.1:0", "127.0.0.1:0"), ("[0:0:0:0:0:0:0:1]:46881", "[::1]:46881")],
)
def test_bind_address_accepts_ipv4_and_bracketed_ipv6(address, expected):
    assert normalize_listen(address) == expected


@pytest.mark.parametrize("address", ["localhost:6881", "::1:6881", "[127.0.0.1]:6881", "127.0.0.1:65536"])
def test_bind_address_rejects_unsupported_endpoints(address):
    with pytest.raises(FQDBError, match="BT listen address"):
        normalize_listen(address)


def test_explicit_cli_listener_binds_and_cannot_change_during_native_session(data_root):
    _, schema, _ = setup_store(data_root)
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    listener = f"127.0.0.1:{port}"
    client = Client(data_root)

    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "fqdb", "-d", str(data_root), "provider", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )

    try:
        started = cli("start", "--dataset", schema.id + "/1d", "--listen", listener)
        assert started.returncode == 0, started.stderr
        result = json.loads(started.stdout)
        assert result["provider"] is True
        assert result["listen_port"] == port
        assert result["settings"]["listen"] == listener

        rejected = cli("start", "--listen", "127.0.0.1:0")
        assert rejected.returncode != 0
        assert "Stop active BitTorrent work before changing listen or discovery" in rejected.stderr
        current = client.get("/api/status")
        assert current["provider"] is True
        assert current["listen_port"] == port
        assert current["settings"]["listen"] == listener
    finally:
        if client.endpoint():
            client.call("shutdown")
