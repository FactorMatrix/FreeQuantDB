"""Explicit synthetic gateway sessions; never resurrect an exited test owner."""

from contextlib import contextmanager
import threading

import httpx

from fqdb.config import Config


def loopback_config(root):
    config = Config(root).ensure()
    config.update(listen="127.0.0.1:0", discovery=False)
    return config


@contextmanager
def gateway_session(client):
    session = client.call("gateway_open")
    endpoint = client.endpoint()
    assert endpoint is not None
    session_id = session["session_id"]
    stopped = threading.Event()

    def send(action):
        try:
            with httpx.Client(trust_env=False, timeout=3) as http:
                return http.post(
                    endpoint["url"] + "/api/action/" + action,
                    json={"session_id": session_id, "browser": False},
                    headers={"Authorization": "Bearer " + endpoint["token"]},
                )
        except httpx.HTTPError:
            return None

    def heartbeat():
        while not stopped.wait(2):
            response = send("gateway_heartbeat")
            if response is None or not response.is_success:
                return

    thread = threading.Thread(target=heartbeat, name="synthetic-gateway", daemon=True)
    thread.start()
    try:
        yield session
    finally:
        stopped.set()
        thread.join(timeout=4)
        send("gateway_close")
