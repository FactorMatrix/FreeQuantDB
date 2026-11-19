"""A closing idle worker may reject a request; accepted mutations never replay."""

import httpx
import pytest

from fqdb.service.client import Client
from fqdb.util import FQDBError


def rpc_client(monkeypatch, handler):
    client = object.__new__(Client)
    monkeypatch.setattr(
        client,
        "endpoint",
        lambda: {"url": "http://127.0.0.1:12345", "token": "SYNTHETIC-retry-test"},
    )
    real_client = httpx.Client
    monkeypatch.setattr(
        "fqdb.service.client.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr("fqdb.service.client.time.sleep", lambda _: None)
    return client


@pytest.mark.parametrize("rejection", ["connect", "idle_shutdown"])
def test_retry_unaccepted_mutation_once(monkeypatch, rejection):
    attempts, accepted = [], []

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            if rejection == "connect":
                raise httpx.ConnectError("worker closed its listener", request=request)
            return httpx.Response(503, json={"error": "worker_stopping", "retryable": True})
        accepted.append(request.content)
        return httpx.Response(200, json={"registered": True})

    client = rpc_client(monkeypatch, handler)
    assert client.call("register", {"id": "SYNTHETIC"}) == {"registered": True}
    assert len(attempts) == 2
    assert len(accepted) == 1
    assert attempts[0].content == attempts[1].content


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ReadError])
def test_accepted_mutation_with_lost_response_is_never_replayed(monkeypatch, failure):
    accepted = []

    def handler(request):
        accepted.append(request.content)
        raise failure("mutation committed; response unavailable", request=request)

    client = rpc_client(monkeypatch, handler)
    with pytest.raises(failure):
        client.call("publish", {"input_path": "SYNTHETIC.csv"})
    assert len(accepted) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"error": "worker_stopping", "retryable": False},
        {"error": "upstream_unavailable", "retryable": True},
        {"error": "worker_stopping"},
    ],
)
def test_arbitrary_503_does_not_replay_mutation(monkeypatch, body):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503, json=body)

    client = rpc_client(monkeypatch, handler)
    with pytest.raises(FQDBError):
        client.call("register", {"id": "SYNTHETIC"})
    assert len(attempts) == 1


def test_repeated_idle_shutdown_rejection_has_bounded_retry(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503, json={"error": "worker_stopping", "retryable": True})

    client = rpc_client(monkeypatch, handler)
    with pytest.raises(FQDBError, match="before accepting"):
        client.call("register", {"id": "SYNTHETIC"})
    assert len(attempts) == 2


def test_passive_read_retries_idle_shutdown_rejection(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(503, json={"error": "worker_stopping", "retryable": True})
        return httpx.Response(200, json={"provider": False})

    client = rpc_client(monkeypatch, handler)
    assert client.get("/api/status") == {"provider": False}
    assert len(attempts) == 2


@pytest.mark.parametrize("persistent", [False, True])
def test_gateway_open_returns_the_endpoint_that_accepted_its_lease(monkeypatch, persistent):
    attempts = []
    worker = object.__new__(Client)
    old = {"url": "http://127.0.0.1:12345", "token": "SYNTHETIC-replaced-worker"}
    new = {"url": old["url"], "token": "SYNTHETIC-replacement-worker"}
    ports = []
    endpoints = iter([old, new])

    def ensure(port):
        ports.append(port)
        return next(endpoints)

    def post(endpoint, action, args, timeout):
        attempts.append(endpoint)
        assert action == "gateway_open" and args == {"persistent": persistent} and timeout == 120
        if endpoint == old:
            raise httpx.ConnectError("worker closed its listener")
        return {"session_id": "SYNTHETIC-lease"}

    monkeypatch.setattr(worker, "ensure", ensure)
    monkeypatch.setattr(worker, "_post", post)
    monkeypatch.setattr("fqdb.service.client.time.sleep", lambda _: None)
    assert worker.open_gateway(port=12345, persistent=persistent) == (new, {"session_id": "SYNTHETIC-lease"})
    assert attempts == [old, new]
    assert ports == [12345, 12345]
