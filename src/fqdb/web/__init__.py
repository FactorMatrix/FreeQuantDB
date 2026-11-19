"""Foreground gateway to the local data workbench."""

import time
import webbrowser

import httpx

from fqdb import api
from fqdb.startup import autostart_enabled, select_client


def run(*, data_dir=None, open_browser=True, port=0):
    """Open the workbench and activate the independent BT service."""
    automatic = autostart_enabled()
    client = (
        api.client()
        if data_dir is None
        else select_client(data_dir, persist=True, opens_service=automatic)[0]
    )
    endpoint, session = client.open_gateway(port=port, persistent=automatic)
    session_id = session["session_id"]
    url = endpoint["url"] + "/?session=" + session_id
    headers = {"Authorization": "Bearer " + endpoint["token"]}
    print(f"FQDB gateway: {url}", flush=True)
    try:
        if open_browser:
            webbrowser.open(url)
        with httpx.Client(trust_env=False, timeout=5, headers=headers) as connection:
            while True:
                response = connection.post(
                    endpoint["url"] + "/api/action/gateway_heartbeat",
                    json={"session_id": session_id},
                )
                if response.status_code == 503 and response.json().get("error") == "worker_stopping":
                    break
                response.raise_for_status()
                if not response.json()["active"]:
                    break
                time.sleep(1)
    except (KeyboardInterrupt, httpx.ConnectError):
        pass
    finally:
        # Never resurrect a worker during gateway cleanup.
        try:
            with httpx.Client(trust_env=False, timeout=5, headers=headers) as connection:
                connection.post(
                    endpoint["url"] + "/api/action/gateway_close", json={"session_id": session_id}
                )
        except httpx.HTTPError:
            pass
    return endpoint["url"]
